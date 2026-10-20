"""Local HTTP/WebSocket API and the dashboard host (ADR-0008).

Module layout:
- `schemas`  : response models (every metric-like value is a `Sourced`, ADR-0010);
- `context`  : `AppContext`, the single owner of runtime state that the API exposes;
- `security` : per-launch token, Host/Origin checks, log redaction of the token;
- `live`     : fan-out of push messages to WebSocket clients;
- `pages`    : `index.html` token injection and the "bundle not built" fallback page;
- `app`      : `create_app(context)` FastAPI factory (testable without uvicorn);
- `server`   : `run(settings)` — process entry point used by `tsclaude app`.
"""
