"""Write-side lake API for importers: staging and publishing parts, committing datasets.

Importers use this module instead of `store.lake` (L-07 b: only `store/` imports the lake
internals). It exposes no way to read rows back.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tsclaude.store.lake import (
    SCHEMAS,
    TIME_KEY,
    ColumnType,
    Lake,
    LakeKind,
    PartCodec,
    PartInfo,
    Row,
    Staging,
    month_is_sealed,
)

__all__ = [
    "SCHEMAS",
    "TIME_KEY",
    "ColumnType",
    "LakeKind",
    "LakeWriter",
    "PartCodec",
    "PartInfo",
    "Row",
    "Staging",
    "month_is_sealed",
]


class LakeWriter:
    def __init__(self, data_dir: Path | str, codec: PartCodec | None = None) -> None:
        self._lake = Lake(data_dir, codec)

    @property
    def data_dir(self) -> Path:
        return self._lake.data_dir

    def begin(self) -> Staging:
        """A staging area for one import; `commit()` publishes it with its manifest."""
        return self._lake.begin()

    def dataset_ids(self) -> list[str]:
        return self._lake.dataset_ids()

    def manifest(self, dataset_id: str) -> dict[str, Any]:
        return self._lake.manifest(dataset_id)

    def part_paths(self) -> Sequence[str]:
        return [ip.part.path for ip in self._lake.index()]

    def refresh(self) -> None:
        self._lake.refresh()
