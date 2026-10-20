"""Time-sortable unique identifiers (ULID, Crockford base32, 26 chars)."""

from __future__ import annotations

import secrets

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_MAX = (1 << 48) - 1


def new_ulid(timestamp_ms: int, randomness: int | None = None) -> str:
    """Create a ULID from a millisecond timestamp (48 bits) and 80 random bits."""
    if not 0 <= timestamp_ms <= _TIME_MAX:
        raise ValueError("timestamp out of ULID range")
    rand = secrets.randbits(80) if randomness is None else randomness
    if not 0 <= rand < (1 << 80):
        raise ValueError("randomness must fit in 80 bits")
    value = (timestamp_ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid_timestamp_ms(ulid: str) -> int:
    """Extract the millisecond timestamp encoded in a ULID."""
    if len(ulid) != 26:
        raise ValueError("ULID must have 26 characters")
    value = 0
    for ch in ulid.upper():
        idx = _ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid ULID character {ch!r}")
        value = (value << 5) | idx
    return value >> 80
