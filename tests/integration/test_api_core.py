"""API core tests that need neither FastAPI nor a network: context, read models, guards."""

from __future__ import annotations

import asyncio
import logging
import socket
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin
from urllib.parse import unquote

import pytest
from api_testkit import TOKEN, ContextFactory, make_tampered_db
from pydantic import BaseModel

from tsclaude.api import schemas
from tsclaude.api.context import (
    TransitionError,
    TransitionRefused,
    coverage_gaps,
    new_token,
)
from tsclaude.api.live import LiveHub
from tsclaude.api.pages import (
    fallback_page,
    inject_token,
    is_safe_relative_path,
    resolve_static_file,
)
from tsclaude.api.process import InstanceLock, bind_loopback, dashboard_url
from tsclaude.api.security import (
    TokenRedactionFilter,
    allowed_hosts,
    allowed_origins,
    origin_allowed,
    token_matches,
)
from tsclaude.contracts.provenance import Sourced
from tsclaude.contracts.timeutil import ManualClock
from tsclaude.runtime.sessions import ObservationInterval

# ---------------------------------------------------------------------- ADR-0010 contract
# Plain (non-Sourced) fields allowed on top-level read models: identity and metadata only.
_PLAIN_ALLOWED = {
    "StatusResponse": {"app_version", "server_time", "heartbeat_interval_s"},
    "CoverageResponse": {"generated_at"},
    "EventsResponse": {"generated_at"},
    "HealthResponse": {"ok", "app", "version"},  # liveness only, no metrics
    "QuitResponse": {"accepted"},
}


def _is_sourced(annotation: Any) -> bool:
    if isinstance(annotation, type) and issubclass(annotation, Sourced):
        return True
    origin = get_origin(annotation)
    if origin is dict:
        return _is_sourced(get_args(annotation)[1])
    if origin in (Union, types.UnionType):
        return False
    return False


@pytest.mark.parametrize("model_name", sorted(_PLAIN_ALLOWED))
def test_every_read_model_field_carries_provenance(model_name: str) -> None:
    model: type[BaseModel] = getattr(schemas, model_name)
    for name, field in model.model_fields.items():
        if name in _PLAIN_ALLOWED[model_name]:
            continue
        assert _is_sourced(field.annotation), f"{model_name}.{name} lacks provenance (ADR-0010)"


# ---------------------------------------------------------------------- context lifecycle
def test_context_start_opens_session_and_verifies_chain(make_context: ContextFactory) -> None:
    ctx = make_context()
    status = ctx.status()
    assert status.session.value is not None
    assert status.chain.value is not None
    assert status.chain.value.ok
    assert status.monitoring.value is not None
    assert status.monitoring.value.state == "STOPPED"
    assert status.heartbeat.provenance == "LIVE"
    assert all(s.provenance == "UNAVAILABLE" and s.reason for s in status.streams.values())


def test_transitions_raise_typed_errors(make_context: ContextFactory) -> None:
    ctx = make_context()
    with pytest.raises(TransitionError) as info:
        ctx.stop_monitoring()
    assert info.value.kind is TransitionRefused.INVALID
    ctx.start_monitoring()
    with pytest.raises(TransitionError) as info:
        ctx.start_monitoring()
    assert info.value.kind is TransitionRefused.INVALID


def test_tampered_chain_locks_monitoring(
    db_path: Path, clock: ManualClock, make_context: ContextFactory
) -> None:
    make_tampered_db(db_path, clock)
    ctx = make_context()
    chain = ctx.status().chain.value
    assert chain is not None
    assert not chain.ok
    assert chain.first_bad_seq == 1
    with pytest.raises(TransitionError) as info:
        ctx.start_monitoring()
    assert info.value.kind is TransitionRefused.LOCKED
    # Session still opens (the UI must come up); derived projections are withheld.
    assert ctx.status().session.value is not None
    assert ctx.status().previous_session.provenance == "UNAVAILABLE"
    assert ctx.coverage().gaps.provenance == "UNAVAILABLE"


def test_close_is_idempotent_and_stops_monitoring(make_context: ContextFactory) -> None:
    ctx = make_context()
    ctx.start_monitoring()
    ctx.close("user")
    ctx.close("user")
    types_ = [str(e.type) for e in ctx.store.read()]
    assert types_[-2:] == ["monitoring_stopped", "session_stopped"]
    assert types_.count("session_stopped") == 1
    assert ctx.status().session.provenance == "UNAVAILABLE"
    with pytest.raises(TransitionError):
        ctx.start_monitoring()


def test_heartbeat_goes_stale_without_beats(
    clock: ManualClock, make_context: ContextFactory
) -> None:
    ctx = make_context(heartbeat_interval_s=1.0)
    clock.advance(2_000)
    assert ctx.status().heartbeat.provenance == "LIVE"
    clock.advance(2_000)  # > 3 intervals since the last beat
    stale = ctx.status().heartbeat
    assert stale.provenance == "STALE"
    assert stale.as_of_ms is not None
    ctx.heartbeat()
    assert ctx.status().heartbeat.provenance == "LIVE"


def test_previous_session_clean_and_crashed(
    clock: ManualClock, make_context: ContextFactory
) -> None:
    first = make_context()
    first.start_monitoring()
    clock.advance(5_000)
    first.close("user")
    clock.advance(5_000)
    second = make_context()
    prev = second.status().previous_session.value
    assert prev is not None
    assert prev.outcome == "CLEAN"
    assert prev.monitoring_was_active is True
    assert prev.unobserved_from == "2025-10-09T08:53:25.000Z"
    assert prev.unobserved_to == "2025-10-09T08:53:30.000Z"
    # `second` is never closed: simulate a kill, then restart.
    clock.advance(60_000)
    third = make_context()
    prev = third.status().previous_session.value
    assert prev is not None
    assert prev.outcome == "CRASHED"
    assert prev.monitoring_was_active is False
    assert prev.unobserved_from == "2025-10-09T08:53:30.000Z"


def test_request_shutdown_without_server(make_context: ContextFactory) -> None:
    ctx = make_context()
    assert ctx.request_shutdown("user_quit") is False
    calls: list[int] = []
    ctx.set_shutdown_callback(lambda: calls.append(1))
    assert ctx.request_shutdown("again") is True
    assert calls == [1]
    assert ctx.shutdown_reason == "user_quit"  # first reason wins


def test_events_paging(make_context: ContextFactory) -> None:
    ctx = make_context()
    ctx.start_monitoring()
    ctx.stop_monitoring()
    page = ctx.events(after_seq=None, limit=500).events.value
    assert page is not None
    assert [e.type for e in page.items] == [
        "session_started",
        "monitoring_started",
        "monitoring_stopped",
    ]
    assert page.has_more is False
    tail = ctx.events(after_seq=2, limit=10).events.value
    assert tail is not None
    assert [e.seq for e in tail.items] == [3]


# ---------------------------------------------------------------------- coverage gaps
def _iv(start: int, end: int | None, reason: str | None = "user") -> ObservationInterval:
    return ObservationInterval(start, end, f"M{start}", reason if end is not None else None)


def test_coverage_gaps() -> None:
    assert coverage_gaps([]) == []
    gaps = coverage_gaps([_iv(0, 1_000), _iv(5_000, 6_000, "crash"), _iv(9_000, None)])
    assert [(g.start, g.end, g.after_reason) for g in gaps] == [
        ("1970-01-01T00:00:01.000Z", "1970-01-01T00:00:05.000Z", "user"),
        ("1970-01-01T00:00:06.000Z", "1970-01-01T00:00:09.000Z", "crash"),
    ]
    # not observing now -> an open-ended trailing gap
    tail = coverage_gaps([_iv(0, 1_000)])
    assert [(g.start, g.end) for g in tail] == [("1970-01-01T00:00:01.000Z", None)]
    # overlapping/contained intervals do not produce negative gaps
    assert coverage_gaps([_iv(0, 10_000), _iv(2_000, 3_000), _iv(10_000, None)]) == []


# ---------------------------------------------------------------------- pages
def test_inject_token() -> None:
    doc = "<html><head><title>x</title></head><body></body></html>"
    out = inject_token(doc, "abc")
    assert '<meta name="tsc-token" content="abc"></head>' in out
    again = inject_token(out, "def")
    assert "abc" not in again
    assert again.count("tsc-token") == 1
    assert inject_token("<p>no head</p>", 'a"b').startswith(
        '<meta name="tsc-token" content="a&quot;b">'
    )


def test_resolve_static_file_refuses_traversal(tmp_path: Path) -> None:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "a.js").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
    assert resolve_static_file(root, "assets/a.js") == (root / "assets" / "a.js").resolve()
    for bad in ("../secret.txt", "assets/../../secret.txt", "", "assets", "a\x00b"):
        assert resolve_static_file(root, bad) is None
    unc_and_absolute = (
        "//h/s/x",
        "\\\\h\\s\\x",
        "C:/x",
        "C:x",
        "/etc/passwd",
        unquote("%5C%5Ch%5Cs"),
        "\\\\?\\C:\\x",
        "assets\\a.js",
        "./assets/a.js",
    )
    for bad in unc_and_absolute:
        assert not is_safe_relative_path(bad), bad
        assert resolve_static_file(root, bad) is None, bad


def test_unsafe_paths_never_reach_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[str] = []
    real_resolve = Path.resolve

    def spy(self: Path, strict: bool = False) -> Path:
        touched.append(str(self))
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", spy)
    for bad in ("//attacker/share/x", "\\\\attacker\\share\\x", "C:/x"):
        assert resolve_static_file(tmp_path, bad) is None
    assert touched == []


def test_fallback_page_escapes_status() -> None:
    page = fallback_page({"x": "<script>alert(1)</script>"})
    assert "<script>alert" not in page
    assert "not built" in page


# ---------------------------------------------------------------------- security helpers
def test_origin_and_host_allowlists() -> None:
    assert allowed_hosts(8765) == {"127.0.0.1:8765", "localhost:8765", "[::1]:8765"}
    origins = allowed_origins(8765)
    assert origin_allowed(None, origins)
    assert origin_allowed("http://localhost:8765", origins)
    for bad in ("null", "http://localhost:8766", "https://localhost:8765", "http://evil"):
        assert not origin_allowed(bad, origins)


def test_token_matching_and_generation() -> None:
    assert token_matches(TOKEN, TOKEN)
    assert not token_matches(TOKEN, None)
    assert not token_matches(TOKEN, "")
    assert not token_matches(TOKEN, TOKEN + "x")
    a, b = new_token(), new_token()
    assert a != b
    assert len(a) >= 43


def test_token_redaction_filter() -> None:
    record = logging.LogRecord(
        "uvicorn", logging.INFO, __file__, 1, "WebSocket /ws/live?token=%s", (TOKEN,), None
    )
    assert TokenRedactionFilter(TOKEN).filter(record)
    assert TOKEN not in record.getMessage()


# ---------------------------------------------------------------------- live hub
def test_live_hub_fanout_and_drop_oldest() -> None:
    async def scenario() -> list[dict[str, Any]]:
        hub = LiveHub(max_queue=2)
        queue = hub.subscribe()
        for i in range(3):
            hub.publish({"i": i})
        await asyncio.sleep(0)  # deliveries are scheduled with call_soon_threadsafe
        got = [queue.get_nowait(), queue.get_nowait()]
        hub.unsubscribe(queue)
        hub.publish({"i": 99})
        await asyncio.sleep(0)
        assert queue.empty()
        assert hub.subscriber_count == 0
        return got

    assert asyncio.run(scenario()) == [{"i": 1}, {"i": 2}]


# ---------------------------------------------------------------------- process helpers
def test_instance_lock_is_exclusive(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "tsclaude.lock")
    with pytest.raises(OSError):  # noqa: PT011 - platform-specific errno
        InstanceLock(tmp_path / "tsclaude.lock")
    lock.release()
    lock.release()
    InstanceLock(tmp_path / "tsclaude.lock").release()


def test_bind_loopback_detects_busy_port() -> None:
    first = bind_loopback("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        with pytest.raises(OSError):  # noqa: PT011 - platform-specific errno
            bind_loopback("127.0.0.1", port)
    finally:
        first.close()
    assert isinstance(first, socket.socket)


def test_dashboard_url() -> None:
    assert dashboard_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"
    assert dashboard_url("::1", 8765) == "http://[::1]:8765/"
