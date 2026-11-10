"""Versioned, hashed prompt files (SIGNALS_AND_AGENTS §3: "Prompts are versioned files").

A prompt's identity is ``<role>-v<N>`` plus the SHA-256 of the file bytes; both go into the cache
key and into every review record, so editing a file without bumping the version still changes the
key (and the old reviews stay attributable to the old text).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


class Role(StrEnum):
    ANALYST = "analyst"
    SKEPTIC = "skeptic"


# Pinned versions. Bump by adding a new file (never edit a released one in place).
PROMPT_FILES: dict[Role, tuple[str, str]] = {
    Role.ANALYST: ("analyst-v1", "analyst_v1.md"),
    Role.SKEPTIC: ("skeptic-v1", "skeptic_v1.md"),
}


@dataclass(frozen=True)
class Prompt:
    role: Role
    version: str
    sha256: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.version}@{self.sha256[:12]}"


def load_prompt(role: Role, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    version, filename = PROMPT_FILES[role]
    data = (prompts_dir / filename).read_bytes()
    return Prompt(role, version, hashlib.sha256(data).hexdigest(), data.decode("utf-8"))
