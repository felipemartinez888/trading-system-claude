from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from md_helpers import T0, Sink

from tsclaude.contracts.timeutil import ManualClock
from tsclaude.quality.gaps import GapRegistry
from tsclaude.store.livebars import LiveBarStore


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def bars(tmp_path: Path) -> Iterator[LiveBarStore]:
    store = LiveBarStore(tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture
def gaps(sink: Sink, clock: ManualClock) -> GapRegistry:
    counter = iter(range(10_000))
    return GapRegistry(sink, clock.now_ms, new_id=lambda _t: f"gap-{next(counter):04d}")
