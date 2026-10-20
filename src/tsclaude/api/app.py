"""FastAPI application factory: `create_app(context) -> FastAPI` (testable without uvicorn).

Endpoints (all JSON, ISO-8601 `Z` timestamps, metric-like values as `Sourced`):

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/health` | liveness for the launcher (no metrics) |
| GET  | `/api/status` | session, monitoring, chain integrity, previous session, streams |
| GET  | `/api/coverage` | observation intervals and the unobserved gaps between them |
| GET  | `/api/events?after_seq=&limit=` | event page (newest page if `after_seq` is omitted) |
| POST | `/api/monitoring/start` | token; 409 invalid transition; 423 chain failed / no session |
| POST | `/api/monitoring/stop` | token; 409 invalid transition |
| POST | `/api/app/quit` | token; 202; closes the session, then stops the server |
| WS   | `/ws/live?token=` | pushes `status` on every change and heartbeat |
| GET  | `/`, `/{path}` | built dashboard (token injected into `index.html`) or fallback page |

Host and Origin checks apply to every request (`security.LocalOnlyMiddleware`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from tsclaude.api import pages
from tsclaude.api.context import (
    MAX_EVENTS_LIMIT,
    AppContext,
    TransitionError,
    TransitionRefused,
)
from tsclaude.api.live import LiveMessage
from tsclaude.api.market_schemas import (
    BarsResponse,
    GapsResponse,
    MarketsResponse,
    NotificationsResponse,
    SignalsResponse,
    StreamsHealthResponse,
)
from tsclaude.api.market_views import (
    DEFAULT_BARS_WINDOW_MS,
    bars_response,
    gap_registry,
    gaps_response,
    markets_response,
    notifications_response,
    signals_response,
    streams_health,
)
from tsclaude.api.pages import (
    fallback_page,
    inject_token,
    is_index_path,
    is_safe_relative_path,
    load_index,
)
from tsclaude.api.push import PUSH_INTERVAL_S, MarketPusher
from tsclaude.api.schemas import (
    CoverageResponse,
    ErrorResponse,
    EventsResponse,
    HealthResponse,
    QuitResponse,
    StatusResponse,
)
from tsclaude.api.security import (
    TOKEN_HEADER,
    TOKEN_QUERY_PARAM,
    LocalOnlyMiddleware,
    allowed_hosts,
    allowed_origins,
    token_matches,
)
from tsclaude.contracts.market import Market
from tsclaude.contracts.provenance import Provenance, Sourced
from tsclaude.contracts.timeutil import from_iso, to_iso
from tsclaude.web import STATIC_DIR

log = logging.getLogger(__name__)

APP_NAME = "tsclaude"
MAX_GAPS_LIMIT = 500

# Explicit types: on Windows `mimetypes` reads the registry, which can map .js to text/plain and
# make browsers refuse to execute module scripts.
_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
}

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "missing/invalid token, foreign Origin or Host"},
}
_BAD_REQUEST: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "invalid time range"},
}
_TRANSITION_ERRORS: dict[int | str, dict[str, Any]] = {
    **_ERRORS,
    503: {"model": ErrorResponse, "description": "market data could not be started"},
    409: {"model": ErrorResponse, "description": "invalid transition for the current state"},
    423: {"model": ErrorResponse, "description": "locked: chain verification failed or no session"},
}


def _content_security_policy(port: int) -> str:
    # Bracketed IPv6 hosts are not valid CSP sources (Chrome drops them); 'self' covers same-host
    # WebSockets in current browsers, the explicit entries cover older ones.
    ws_sources = f"ws://127.0.0.1:{port} ws://localhost:{port}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"font-src 'self'; connect-src 'self' {ws_sources}; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def create_app(
    context: AppContext,
    *,
    static_dir: Path = STATIC_DIR,
    heartbeat: bool = True,
    push_interval_s: float | None = PUSH_INTERVAL_S,
    on_started: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> FastAPI:
    """Build the API around an already-started `AppContext`.

    For the app's lifespan (uvicorn, or a `TestClient` used as a context manager):
    - `heartbeat=True` runs the heartbeat loop;
    - `push_interval_s` runs the market-data push loop (None disables it);
    - `on_started` runs as a background task once serving (e.g. auto-start for verification runs).
    On shutdown, market data is stopped and the monitoring interval closed *inside* the event loop
    (the service is async), before the server process records `session_stopped`.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        context.loop = asyncio.get_running_loop()
        tasks: list[asyncio.Task[None]] = []
        if heartbeat:
            tasks.append(asyncio.create_task(_heartbeat_loop(context), name="heartbeat"))
        if push_interval_s is not None and context.supervisor is not None:
            pusher = MarketPusher(context)
            app.state.pusher = pusher
            tasks.append(asyncio.create_task(pusher.run(push_interval_s), name="market-push"))
        if on_started is not None:
            tasks.append(asyncio.create_task(on_started(), name="on-started"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await context.shutdown_monitoring("app_shutdown")
            if context.supervisor is not None:
                await context.supervisor.close()
            context.release_resources()
            context.loop = None

    app = FastAPI(
        title="Trading System Claude — local API",
        version=context.app_version,
        lifespan=lifespan,
        docs_url=None,  # no third-party CDN assets; the OpenAPI JSON stays available
        redoc_url=None,
    )
    app.add_middleware(
        LocalOnlyMiddleware,
        hosts=allowed_hosts(context.port),
        origins=allowed_origins(context.port),
    )

    def require_token(
        x_tsc_token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        if not token_matches(context.token, x_tsc_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="missing or invalid token")

    token_dep = [Depends(require_token)]

    # ------------------------------------------------------------------ reads
    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(ok=True, app=APP_NAME, version=context.app_version)

    @app.get("/api/status", response_model=StatusResponse)
    async def get_status() -> StatusResponse:
        # On the event loop: refresh the Spot/Futures summary from the live service first, so the
        # status never lags the market-data state (threads only read the cached summary).
        if context.supervisor is not None:
            context.supervisor.refresh()
        return context.status()

    @app.get("/api/coverage", response_model=CoverageResponse)
    def get_coverage() -> CoverageResponse:
        return context.coverage()

    @app.get("/api/events", response_model=EventsResponse)
    def get_events(
        after_seq: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_LIMIT)] = 100,
    ) -> EventsResponse:
        return context.events(after_seq=after_seq, limit=limit)

    # ------------------------------------------------------------------ market data reads
    # Async: the market-data service lives on the event loop; reading its snapshot from a worker
    # thread could race with ingestion. Store-only reads run in a thread.
    @app.get("/api/markets", response_model=MarketsResponse)
    async def get_markets() -> MarketsResponse:
        now = context.clock.now_ms()
        if context.supervisor is None:
            return MarketsResponse(
                generated_at=to_iso(now),
                running=Sourced[bool](value=False, provenance=Provenance.SYSTEM, as_of_ms=now),
                rows=[],
            )
        return markets_response(context.supervisor, context.bar_store(), now)

    @app.get("/api/health/streams", response_model=StreamsHealthResponse)
    async def get_streams_health() -> StreamsHealthResponse:
        now = context.clock.now_ms()
        registry = await asyncio.to_thread(gap_registry, context.store, now)
        return streams_health(context.supervisor, registry, now)

    @app.get("/api/gaps", response_model=GapsResponse)
    async def get_gaps(
        limit: Annotated[int, Query(ge=1, le=MAX_GAPS_LIMIT)] = 200,
    ) -> GapsResponse:
        now = context.clock.now_ms()
        registry = await asyncio.to_thread(gap_registry, context.store, now)
        return gaps_response(registry, now, limit)

    @app.get("/api/bars", response_model=BarsResponse, responses=_BAD_REQUEST)
    async def get_bars(
        market: Annotated[Market, Query()],
        symbol: Annotated[str, Query(pattern=r"^[A-Z0-9]{2,20}$")],
        interval: Annotated[str, Query(pattern=r"^1m$")] = "1m",
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: Annotated[str | None, Query()] = None,
    ) -> BarsResponse:
        now = context.clock.now_ms()
        try:
            end = from_iso(to) if to else now
            start = from_iso(from_) if from_ else end - DEFAULT_BARS_WINDOW_MS
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if start >= end:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="'from' must be before 'to'")
        del interval  # only 1m bars are stored (validated by the pattern)
        return await asyncio.to_thread(
            bars_response, context.bar_store(), market, symbol, start, end, now
        )

    @app.get("/api/signals", response_model=SignalsResponse)
    async def get_signals(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> SignalsResponse:
        now = context.clock.now_ms()
        return await asyncio.to_thread(signals_response, context.store, now, limit)

    @app.get("/api/notifications", response_model=NotificationsResponse)
    async def get_notifications(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> NotificationsResponse:
        now = context.clock.now_ms()
        extra = list(context.transient_notifications)
        return await asyncio.to_thread(notifications_response, context.store, now, limit, extra)

    # ------------------------------------------------------------------ commands
    async def _transition(action: Callable[[], Awaitable[StatusResponse]]) -> StatusResponse:
        try:
            return await action()
        except TransitionError as exc:
            code = {
                TransitionRefused.LOCKED: status.HTTP_423_LOCKED,
                TransitionRefused.FAILED: status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.kind, status.HTTP_409_CONFLICT)
            raise HTTPException(code, detail=exc.detail) from exc

    @app.post(
        "/api/monitoring/start",
        response_model=StatusResponse,
        dependencies=token_dep,
        responses=_TRANSITION_ERRORS,
    )
    async def start_monitoring() -> StatusResponse:
        return await _transition(context.start_monitoring)

    @app.post(
        "/api/monitoring/stop",
        response_model=StatusResponse,
        dependencies=token_dep,
        responses=_TRANSITION_ERRORS,
    )
    async def stop_monitoring() -> StatusResponse:
        return await _transition(context.stop_monitoring)

    @app.post(
        "/api/app/quit",
        response_model=QuitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=token_dep,
        responses=_ERRORS,
    )
    async def quit_app() -> QuitResponse:
        log.info("quit requested from the dashboard")
        return await context.request_shutdown("user_quit")

    # ------------------------------------------------------------------ live push
    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        if not token_matches(context.token, websocket.query_params.get(TOKEN_QUERY_PARAM)):
            await websocket.close(code=1008, reason="missing or invalid token")
            return
        await websocket.accept()
        queue = context.hub.subscribe()
        receiver = asyncio.create_task(_drain_client(websocket))
        try:
            await websocket.send_json(context.status_message())
            while True:
                getter: asyncio.Task[LiveMessage] = asyncio.create_task(queue.get())
                waiters: set[asyncio.Task[Any]] = {getter, receiver}
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:  # client went away
                    getter.cancel()
                    break
                message = getter.result()
                await websocket.send_json(message)
                if message.get("type") == "shutdown":
                    await websocket.close(code=1001, reason="server shutting down")
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass  # client disconnected mid-send
        finally:
            context.hub.unsubscribe(queue)
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await receiver

    # ------------------------------------------------------------------ dashboard
    csp = _content_security_policy(context.port)

    def _index() -> Response:
        document = load_index(static_dir)
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if document is None:
            page = fallback_page(context.status().model_dump(mode="json"))
            return HTMLResponse(page, headers=headers)
        headers["Content-Security-Policy"] = csp
        headers["Referrer-Policy"] = "no-referrer"
        return HTMLResponse(inject_token(document, context.token), headers=headers)

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return _index()

    @app.get("/{path:path}", include_in_schema=False)
    def static_file(path: str) -> Response:
        if path.startswith(("api/", "ws/")) or path in ("api", "ws"):
            return JSONResponse({"detail": "Not Found"}, status_code=status.HTTP_404_NOT_FOUND)
        route = path[:-1] if path.endswith("/") else path
        if is_index_path(route):  # any letter case: always with token, CSP and no-store
            return _index()
        if not is_safe_relative_path(route):
            # Absolute, drive, UNC, backslash, dot segments, trailing dot/space or Windows device
            # names: refused before any filesystem access (on Windows resolving a UNC path would
            # open an SMB connection; "nul.js" opens a device).
            return JSONResponse({"detail": "Not Found"}, status_code=status.HTTP_404_NOT_FOUND)
        file = pages.resolve_static_file(static_dir, route)
        if file is not None and pages.is_index_file(static_dir, file):
            # Another name for index.html (e.g. the Windows 8.3 short name INDEX~1.HTM): still
            # served with the token, CSP and no-store, never as a raw file.
            return _index()
        if file is None:
            last = route.rsplit("/", 1)[-1]
            if "." in last:  # a missing asset, not a client-side route
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return _index()
        media_type = _MEDIA_TYPES.get(file.suffix.lower())
        cache = "public, max-age=31536000, immutable" if route.startswith("assets/") else "no-cache"
        return FileResponse(
            file,
            media_type=media_type,
            headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"},
        )

    return app


async def _heartbeat_loop(context: AppContext) -> None:
    interval = context.heartbeat_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(context.heartbeat)
        except Exception:  # keep beating; staleness becomes visible in /api/status
            log.exception("heartbeat failed")


async def _drain_client(websocket: WebSocket) -> None:
    """Consume (and ignore) client messages until the client disconnects."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
