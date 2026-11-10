"""Verified cache of the development-segment maximum-loss measurement (audit WF-4).

`runner.measure_max_loss` simulates the whole development segment for every run (L-10). The
measurement depends only on the strategy (id, version, parameters), the symbols, the backtest
configuration, the cost model, the exchange-interruption registry, the engine versions, the code
and the exact lake parts read, never on the run's own window. A walk-forward evaluates the same
configuration on many windows, so the measurement is cached under a key built from all of these.

Fail closed, never trusted blindly:

- the key includes the code commit; with an unknown commit or a dirty working tree there is no
  key and nothing is cached or read (the measurement is recomputed);
- the key includes the content hash of every lake part the measurement reads (bars, and for
  USD-M funding and mark prices), so any new import or repair changes the key;
- an entry is used only if its stored key body equals the recomputed one and its value hash
  verifies; anything else is a miss, and the fresh measurement replaces the entry;
- the cached value is exactly the dict the measurement returned (JSON floats round-trip
  exactly), so a run with a cache hit has the same manifest and metrics hashes as without.

Entries are written atomically (temporary file, then rename). The cache is an optimisation: it is
safe to delete the directory at any time.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tsclaude.contracts.events import canonical_json

MAX_LOSS_CACHE_VERSION = "max-loss-cache-v1"


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


class MaxLossCache:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.hits = 0
        self.misses = 0

    def _path(self, key_body: Mapping[str, Any]) -> Path:
        return self.root / f"{_sha(dict(key_body))}.json"

    def get(self, key_body: Mapping[str, Any]) -> dict[str, Any] | None:
        """The cached measurement for this key body, or None (missing, unreadable, or failing
        verification)."""
        path = self._path(key_body)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        value = entry.get("value") if isinstance(entry, dict) else None
        if (
            not isinstance(value, dict)
            or entry.get("version") != MAX_LOSS_CACHE_VERSION
            or entry.get("key_body") != json.loads(canonical_json(dict(key_body)))
            or entry.get("value_sha256") != _sha(value)
        ):
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key_body: Mapping[str, Any], value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = json.loads(canonical_json(dict(key_body)))
        stored = json.loads(canonical_json(dict(value)))
        if stored != dict(value):
            raise ValueError("the measurement does not round-trip through JSON; not cached")
        entry = {
            "version": MAX_LOSS_CACHE_VERSION,
            "key_body": body,
            "value": stored,
            "value_sha256": _sha(stored),
        }
        path = self._path(key_body)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(canonical_json(entry) + "\n", encoding="utf-8")
        os.replace(tmp, path)


__all__ = ["MAX_LOSS_CACHE_VERSION", "MaxLossCache"]
