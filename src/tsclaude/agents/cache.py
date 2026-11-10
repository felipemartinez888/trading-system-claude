"""Review cache keyed by ``hash(packet, prompt versions + hashes, model, synthesis version)``.

Re-displaying a review never re-calls the API (SIGNALS_AND_AGENTS §3). Only completed reviews are
cached; failures are not (a later attempt may succeed). A deliberate re-evaluation bypasses the
lookup and *adds* a new record; the newest record for a key is the one served.
Persistence is an append-only JSONL file (or memory only). An unreadable line is skipped and
counted in `errors`: a lost cache entry only costs a re-review, it can never approve anything.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from tsclaude.contracts.events import canonical_json

from .review import AgentReview


def review_key(
    packet_hash: str, analyst_ref: str, skeptic_ref: str, model: str, synthesis_version: str
) -> str:
    material = canonical_json(
        {
            "packet": packet_hash,
            "analyst": analyst_ref,
            "skeptic": skeptic_ref,
            "model": model,
            "synthesis": synthesis_version,
        }
    )
    return hashlib.sha256(material.encode()).hexdigest()


class ReviewCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.errors = 0
        self._lock = threading.Lock()
        self._by_key: dict[str, AgentReview] = {}
        if path is not None and path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
                self.errors += 1
            for line in lines:
                if not line.strip():
                    continue
                try:
                    review = AgentReview.model_validate(json.loads(line))
                except ValueError:
                    self.errors += 1
                    continue
                self._by_key[review.review_key] = review

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, key: str) -> AgentReview | None:
        with self._lock:
            return self._by_key.get(key)

    def put(self, review: AgentReview) -> None:
        with self._lock:
            self._by_key[review.review_key] = review
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(review.model_dump_json() + "\n")
                except OSError:
                    self.errors += 1
