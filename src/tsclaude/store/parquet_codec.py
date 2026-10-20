"""Parquet part codec (pyarrow). Columns are typed explicitly; partition values live in the path."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from tsclaude.store.lake import ColumnType, Row

_ARROW: dict[ColumnType, Any] = {
    "int": pa.int64(),
    "float": pa.float64(),
    "float?": pa.float64(),
    "str": pa.string(),
}


class ParquetCodec:
    extension = "parquet"

    def __init__(self, compression: str = "zstd") -> None:
        self.compression = compression

    def _schema(self, schema: Sequence[tuple[str, ColumnType]]) -> Any:
        return pa.schema(
            [pa.field(name, _ARROW[ctype], nullable=(ctype == "float?")) for name, ctype in schema]
        )

    def write(
        self, path: Path, schema: Sequence[tuple[str, ColumnType]], rows: Sequence[Row]
    ) -> None:
        columns = {name: [row[name] for row in rows] for name, _ in schema}
        table = pa.Table.from_pydict(columns, schema=self._schema(schema))
        pq.write_table(table, str(path), compression=self.compression, write_statistics=True)

    def read(self, path: Path, schema: Sequence[tuple[str, ColumnType]]) -> list[Row]:
        table = pq.read_table(str(path), columns=[name for name, _ in schema])
        if not table.schema.equals(self._schema(schema), check_metadata=False):
            raise ValueError(f"unexpected schema in {path}: {table.schema}")
        rows: list[Row] = table.to_pylist()
        return rows
