from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_testkit import T0, ContextFactory

from tsclaude.contracts.timeutil import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def make_context(db_path: Path, clock: ManualClock) -> Iterator[ContextFactory]:
    factory = ContextFactory(db_path, clock)
    yield factory
    factory.close_all()
