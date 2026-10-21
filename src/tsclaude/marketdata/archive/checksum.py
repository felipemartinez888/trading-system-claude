"""`.CHECKSUM` files: `<sha256-hex>  <file name>` (sha256sum format), verified before any use."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")


class ChecksumError(ValueError):
    """Checksum file malformed, for another file, or not matching the data. Always a hard error."""


def parse_checksum(text: str, expected_name: str) -> str:
    """Return the lower-case SHA-256 for `expected_name` from a .CHECKSUM file's text."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != 1:
        raise ChecksumError(f"checksum file for {expected_name} must have exactly one line")
    m = _LINE.match(lines[0].strip())
    if m is None:
        raise ChecksumError(f"checksum file for {expected_name} is malformed")
    if Path(m.group(2)).name != expected_name:
        raise ChecksumError(f"checksum file names {m.group(2)!r}, expected {expected_name!r}")
    return m.group(1).lower()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def verify(data: bytes, expected_sha256: str, name: str) -> str:
    actual = sha256_bytes(data)
    if actual != expected_sha256.lower():
        raise ChecksumError(
            f"checksum mismatch for {name}: expected {expected_sha256.lower()}, got {actual}"
        )
    return actual
