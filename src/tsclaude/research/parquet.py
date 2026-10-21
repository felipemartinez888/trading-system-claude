"""Minimal, deterministic Parquet writer/reader for run artifacts (trades, equity).

Why not pyarrow: the L-07 import guard forbids `pyarrow` outside `store/`, and `store/` exposes no
artifact writer (change requested). This module writes a strict subset of Apache Parquet 2 /
format version 1 that any reader (pyarrow, DuckDB, Polars) understands:

- one row group, one PLAIN-encoded uncompressed DATA_PAGE (v1) per column;
- physical types BOOLEAN, INT64, DOUBLE, BYTE_ARRAY (UTF8 converted type);
- columns REQUIRED, or OPTIONAL (definition levels RLE-encoded, bit width 1);
- FileMetaData in the Thrift compact protocol; no statistics, no key/value metadata.

Output bytes depend only on the schema and rows (no timestamps, no library versions), so the same
run writes byte-identical files on every platform. `read_table` reads back files written here
(it is not a general Parquet reader).

Column kinds: ``"int"``, ``"float"``, ``"bool"``, ``"str"`` and their optional forms with ``?``.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

MAGIC = b"PAR1"
CREATED_BY = "tsclaude research.parquet v1"

# parquet.thrift enums
_BOOLEAN, _INT64, _DOUBLE, _BYTE_ARRAY = 0, 2, 5, 6
_REQUIRED, _OPTIONAL = 0, 1
_UTF8 = 0
_PLAIN, _RLE = 0, 3
_UNCOMPRESSED = 0
_DATA_PAGE = 0

_PHYSICAL = {"int": _INT64, "float": _DOUBLE, "bool": _BOOLEAN, "str": _BYTE_ARRAY}

# Thrift compact types
_CT_TRUE, _CT_FALSE, _CT_BYTE, _CT_I16, _CT_I32, _CT_I64 = 1, 2, 3, 4, 5, 6
_CT_DOUBLE, _CT_BINARY, _CT_LIST, _CT_SET, _CT_MAP, _CT_STRUCT = 7, 8, 9, 10, 11, 12

type Schema = Sequence[tuple[str, str]]


# ---------------------------------------------------------------- thrift compact (write)
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


class _Struct:
    """Builds one compact-protocol struct; fields must be added in ascending id order."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._last = 0

    def _header(self, fid: int, ctype: int) -> None:
        delta = fid - self._last
        if not 0 < delta <= 15:
            raise ValueError("fields must be added in ascending order (delta 1..15)")
        self.buf.append((delta << 4) | ctype)
        self._last = fid

    def i32(self, fid: int, v: int) -> _Struct:
        self._header(fid, _CT_I32)
        self.buf += _varint(_zigzag(v))
        return self

    def i64(self, fid: int, v: int) -> _Struct:
        self._header(fid, _CT_I64)
        self.buf += _varint(_zigzag(v))
        return self

    def string(self, fid: int, v: str) -> _Struct:
        data = v.encode("utf-8")
        self._header(fid, _CT_BINARY)
        self.buf += _varint(len(data)) + data
        return self

    def struct(self, fid: int, s: _Struct) -> _Struct:
        self._header(fid, _CT_STRUCT)
        self.buf += s.done()
        return self

    def _list_header(self, fid: int, etype: int, size: int) -> None:
        self._header(fid, _CT_LIST)
        if size < 15:
            self.buf.append((size << 4) | etype)
        else:
            self.buf.append(0xF0 | etype)
            self.buf += _varint(size)

    def list_i32(self, fid: int, items: Sequence[int]) -> _Struct:
        self._list_header(fid, _CT_I32, len(items))
        for v in items:
            self.buf += _varint(_zigzag(v))
        return self

    def list_string(self, fid: int, items: Sequence[str]) -> _Struct:
        self._list_header(fid, _CT_BINARY, len(items))
        for v in items:
            data = v.encode("utf-8")
            self.buf += _varint(len(data)) + data
        return self

    def list_struct(self, fid: int, items: Sequence[_Struct]) -> _Struct:
        self._list_header(fid, _CT_STRUCT, len(items))
        for s in items:
            self.buf += s.done()
        return self

    def done(self) -> bytes:
        return bytes(self.buf) + b"\x00"


# ---------------------------------------------------------------- encoding
def _kind(kind: str) -> tuple[str, bool]:
    base, optional = kind.removesuffix("?"), kind.endswith("?")
    if base not in _PHYSICAL:
        raise ValueError(f"unsupported column kind {kind!r}")
    return base, optional


def _plain(base: str, values: Sequence[Any]) -> bytes:
    if base == "int":
        return np.asarray(values, dtype="<i8").tobytes()
    if base == "float":
        return np.asarray(values, dtype="<f8").tobytes()
    if base == "bool":
        bits = np.asarray(values, dtype=np.uint8)
        return np.packbits(bits, bitorder="little").tobytes()
    out = bytearray()
    for v in values:
        data = str(v).encode("utf-8")
        out += struct.pack("<I", len(data)) + data
    return bytes(out)


def _def_levels(defined: Sequence[bool]) -> bytes:
    """RLE runs (bit width 1), prefixed with their byte length (data page v1)."""
    body = bytearray()
    i = 0
    while i < len(defined):
        j = i
        while j < len(defined) and defined[j] == defined[i]:
            j += 1
        body += _varint((j - i) << 1) + bytes([1 if defined[i] else 0])
        i = j
    return struct.pack("<I", len(body)) + bytes(body)


def write_table(path: Path | str, schema: Schema, rows: Sequence[Mapping[str, Any]]) -> None:
    names = [n for n, _ in schema]
    if len(set(names)) != len(names) or not names:
        raise ValueError("column names must be unique and non-empty")
    n_rows = len(rows)
    out = bytearray(MAGIC)
    chunks: list[_Struct] = []
    total = 0
    for name, kind in schema:
        base, optional = _kind(kind)
        values = [r[name] for r in rows]
        if not optional and any(v is None for v in values):
            raise ValueError(f"column {name!r} is required but has None values")
        defined = [v is not None for v in values]
        page = (_def_levels(defined) if optional else b"") + _plain(
            base, [v for v in values if v is not None]
        )
        header = (
            _Struct()
            .i32(1, _DATA_PAGE)
            .i32(2, len(page))
            .i32(3, len(page))
            .struct(
                5,
                _Struct().i32(1, n_rows).i32(2, _PLAIN).i32(3, _RLE).i32(4, _RLE),
            )
            .done()
        )
        offset = len(out)
        out += header + page
        size = len(header) + len(page)
        total += size
        meta = (
            _Struct()
            .i32(1, _PHYSICAL[base])
            .list_i32(2, [_PLAIN, _RLE])
            .list_string(3, [name])
            .i32(4, _UNCOMPRESSED)
            .i64(5, n_rows)
            .i64(6, size)
            .i64(7, size)
            .i64(9, offset)
        )
        chunks.append(_Struct().i64(2, offset).struct(3, meta))
    elements = [_Struct().string(4, "schema").i32(5, len(schema))]
    for name, kind in schema:
        base, optional = _kind(kind)
        el = _Struct().i32(1, _PHYSICAL[base]).i32(3, _OPTIONAL if optional else _REQUIRED)
        el.string(4, name)
        if base == "str":
            el.i32(6, _UTF8)
        elements.append(el)
    footer = _Struct().i32(1, 1).list_struct(2, elements).i64(3, n_rows)
    groups = [] if n_rows == 0 else [_Struct().list_struct(1, chunks).i64(2, total).i64(3, n_rows)]
    footer.list_struct(4, groups).string(6, CREATED_BY)
    meta_bytes = footer.done()
    out += meta_bytes + struct.pack("<I", len(meta_bytes)) + MAGIC
    Path(path).write_bytes(bytes(out))


# ---------------------------------------------------------------- thrift compact (read)
class _Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def varint(self) -> int:
        shift = result = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def value(self, ctype: int) -> Any:
        if ctype in (_CT_TRUE, _CT_FALSE):
            return ctype == _CT_TRUE
        if ctype == _CT_BYTE:
            return self.byte()
        if ctype in (_CT_I16, _CT_I32, _CT_I64):
            return self.zigzag()
        if ctype == _CT_DOUBLE:
            v = struct.unpack_from("<d", self.data, self.pos)[0]
            self.pos += 8
            return v
        if ctype == _CT_BINARY:
            n = self.varint()
            v = self.data[self.pos : self.pos + n]
            self.pos += n
            return v
        if ctype in (_CT_LIST, _CT_SET):
            h = self.byte()
            size, etype = h >> 4, h & 0x0F
            if size == 15:
                size = self.varint()
            if etype in (_CT_TRUE, _CT_FALSE):  # booleans in lists are one byte each
                return [self.byte() == 1 for _ in range(size)]
            return [self.value(etype) for _ in range(size)]
        if ctype == _CT_STRUCT:
            return self.struct()
        raise ValueError(f"unsupported thrift compact type {ctype}")

    def struct(self) -> dict[int, Any]:
        out: dict[int, Any] = {}
        last = 0
        while True:
            h = self.byte()
            if h == 0:
                return out
            delta, ctype = h >> 4, h & 0x0F
            fid = last + delta if delta else self.zigzag()
            out[fid] = self.value(ctype)
            last = fid


def read_table(path: Path | str) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Schema and rows of a file written by `write_table`."""
    data = Path(path).read_bytes()
    if data[:4] != MAGIC or data[-4:] != MAGIC:
        raise ValueError("not a Parquet file")
    (meta_len,) = struct.unpack_from("<I", data, len(data) - 8)
    meta = _Reader(data, len(data) - 8 - meta_len).struct()
    n_rows = int(meta[3])
    kinds = {_INT64: "int", _DOUBLE: "float", _BOOLEAN: "bool", _BYTE_ARRAY: "str"}
    schema: list[tuple[str, str]] = []
    for el in meta[2][1:]:
        kind = kinds[el[1]] + ("?" if el.get(3) == _OPTIONAL else "")
        schema.append((el[4].decode("utf-8"), kind))
    columns: dict[str, list[Any]] = {}
    groups = meta.get(4, [])
    for (name, kind), chunk in zip(schema, groups[0][1] if groups else [], strict=False):
        base, optional = _kind(kind)
        r = _Reader(data, int(chunk[3][9]))
        r.struct()  # page header
        pos = r.pos
        defined = [True] * n_rows
        if optional:
            (length,) = struct.unpack_from("<I", data, pos)
            lr = _Reader(data, pos + 4)
            defined = []
            while lr.pos < pos + 4 + length:
                run = lr.varint() >> 1
                defined += [lr.byte() == 1] * run
            pos += 4 + length
        k = sum(defined)
        vals: list[Any]
        if base == "int":
            vals = np.frombuffer(data, dtype="<i8", count=k, offset=pos).tolist()
        elif base == "float":
            vals = np.frombuffer(data, dtype="<f8", count=k, offset=pos).tolist()
        elif base == "bool":
            raw = np.frombuffer(data, dtype=np.uint8, count=(k + 7) // 8, offset=pos)
            vals = [bool(b) for b in np.unpackbits(raw, bitorder="little")[:k]]
        else:
            vals = []
            for _ in range(k):
                (n,) = struct.unpack_from("<I", data, pos)
                vals.append(data[pos + 4 : pos + 4 + n].decode("utf-8"))
                pos += 4 + n
        it = iter(vals)
        columns[name] = [next(it) if d else None for d in defined]
    rows = [{name: columns[name][i] for name, _ in schema} for i in range(n_rows)] if groups else []
    return schema, rows
