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
| POST | `/api/app/quit` | token; 202, graceful shutdown |
| WS   | `/ws/live?token=` | pushes `status` on every change and heartbeat |
| GET  | `/`, `/{path}` | built dashboard (token injected into `index.html`) or fallback page |

Host and Origin checks apply to every request (`security.LocalOnlyMiddleware`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from tsclaude.api.context import (
    MAX_EVENTS_LIMIT,
    AppContext,
    TransitionError,
    TransitionRefused,
)
from tsclaude.api.live import LiveMessage
from tsclaude.api.pages import fallback_page, inject_token, load_index, resolve_static_file
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
from tsclaude.web import STATIC_DIR

log = logging.getLogger(__name__)

APP_NAME = "tsclaude"

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

_ERRORS: dict[int | str, dict[str, object]] = {
    403: {"model": ErrorResponse, "description": "missing/invalid token, foreign Origin or Host"},
}
_TRANSITION_ERRORS: dict[int | str, dict[str, object]] = {
    **_ERRORS,
    409: {"model": ErrorResponse, "description": "invalid transition for the current state"},
    423: {"model": ErrorResponse, "description": "locked: chain verification failed or no session"},
}


def _content_security_policy(port: int) -> str:
    ws_sources = " ".join(f"ws://{host}" for host in sorted(allowed_hosts(port)))
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
) -> FastAPI:
    """Build the API around an already-started `AppContext`.

    `heartbeat=True` runs the heartbeat loop for the app's lifespan (uvicorn, or a `TestClient`
    used as a context manager).
    """

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_heartbeat_loop(context)) if heartbeat else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
    def get_status() -> StatusResponse:
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

    # ------------------------------------------------------------------ commands
    def _transition(action: Callable[[], StatusResponse]) -> StatusResponse:
        try:
            return action()
        except TransitionError as exc:
            code = (
                status.HTTP_423_LOCKED
                if exc.kind is TransitionRefused.LOCKED
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(code, detail=exc.detail) from exc

    @app.post(
        "/api/monitoring/start",
        response_model=StatusResponse,
        dependencies=token_dep,
        responses=_TRANSITION_ERRORS,
    )
    def start_monitoring() -> StatusResponse:
        return _transition(context.start_monitoring)

    @app.post(
        "/api/monitoring/stop",
        response_model=StatusResponse,
        dependencies=token_dep,
        responses=_TRANSITION_ERRORS,
    )
    def stop_monitoring() -> StatusResponse:
        return _transition(context.stop_monitoring)

    @app.post(
        "/api/app/quit",
        response_model=QuitResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=token_dep,
        responses=_ERRORS,
    )
    def quit_app() -> QuitResponse:
        log.info("quit requested from the dashboard")
        return QuitResponse(accepted=context.request_shutdown("user_quit"))

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
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
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
        if path == "index.html":
            return _index()
        file = resolve_static_file(static_dir, path)
        if file is None:
            last = path.rsplit("/", 1)[-1]
            if "." in last:  # a missing asset, not a client-side route
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return _index()
        media_type = _MEDIA_TYPES.get(file.suffix.lower())
        cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
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
