"""Local-only request guards (ARCHITECTURE §7).

Threat model: the API listens on loopback, but any web page open in the user's browser can still
send requests to `127.0.0.1:<port>`. Defences:

1. **Host allowlist** (all HTTP and WebSocket requests): only `127.0.0.1:<port>`,
   `localhost:<port>` and `[::1]:<port>`. Blocks DNS-rebinding pages, which would otherwise be
   "same-origin" with us and could read the token out of `index.html`.
2. **Origin check** (all requests that carry an `Origin`): must be one of
   `http://127.0.0.1:<port>`, `http://localhost:<port>`, `http://[::1]:<port>`. `null` is rejected.
3. **Per-launch token** (mutating requests via `X-TSC-Token`; WebSocket via `?token=`): random
   `secrets.token_urlsafe(32)`, injected into the served `index.html` only, compared in constant
   time, never logged (see `TokenRedactionFilter`).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_HEADER = "X-TSC-Token"  # noqa: S105 - a header name, not a secret
TOKEN_QUERY_PARAM = "token"  # noqa: S105 - a parameter name, not a secret
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset(f"{name}:{port}" for name in _LOOPBACK_NAMES)


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset(f"http://{host}" for host in allowed_hosts(port))


def token_matches(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return secrets.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def origin_allowed(origin: str | None, origins: Iterable[str]) -> bool:
    """A missing Origin is allowed (non-browser clients); a present one must match exactly."""
    if origin is None:
        return True
    return origin in frozenset(origins)


class LocalOnlyMiddleware:
    """Pure ASGI middleware enforcing the Host allowlist and the Origin check.

    Implemented at the ASGI level (not `BaseHTTPMiddleware`) so it also covers WebSockets.
    """

    def __init__(self, app: ASGIApp, *, hosts: Iterable[str], origins: Iterable[str]) -> None:
        self.app = app
        self.hosts = frozenset(h.lower() for h in hosts)
        self.origins = frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "").lower()
        problem: str | None = None
        if host not in self.hosts:
            problem = "forbidden host"
        elif not origin_allowed(headers.get("origin"), self.origins):
            problem = "forbidden origin"
        if problem is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": 1008, "reason": problem})
            return
        body = json.dumps({"detail": problem}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TokenRedactionFilter(logging.Filter):
    """Removes the per-launch token from any log record (e.g. a WebSocket URL with `?token=`)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, "***REDACTED***")
            record.args = None
        return True
