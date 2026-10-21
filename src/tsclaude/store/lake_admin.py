"""Integrity and catalog tools over the lake (no row access): verify datasets, build the catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tsclaude.store.lake import Lake, PartCodec


def dataset_ids(data_dir: Path | str, codec: PartCodec | None = None) -> list[str]:
    return Lake(data_dir, codec).dataset_ids()


def verify(
    data_dir: Path | str, dataset_ids_: list[str] | None = None, codec: PartCodec | None = None
) -> dict[str, Any]:
    """Verify the given datasets (default: all). Reports contain hashes and counts only."""
    lake = Lake(data_dir, codec)
    ids = lake.dataset_ids() if dataset_ids_ is None else dataset_ids_
    cache = Path(data_dir) / "archive_cache"
    reports = [lake.verify(d, raw_cache_dir=cache) for d in ids]
    return {"ok": all(r["ok"] for r in reports), "verified": len(reports), "reports": reports}


def build_catalog(data_dir: Path | str, codec: PartCodec | None = None) -> str | None:
    """(Re)build catalog.duckdb without sealed parts. None if duckdb is not installed."""
    try:
        return str(Lake(data_dir, codec).build_catalog())
    except ModuleNotFoundError:
        return None
