from __future__ import annotations

from pathlib import Path

import pytest
from arc_helpers import FakeArchive, FakeRest, JsonLinesCodec

from tsclaude.store.lake import Lake


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "data", JsonLinesCodec())


@pytest.fixture
def archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def rest() -> FakeRest:
    return FakeRest()
