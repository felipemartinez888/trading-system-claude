"""Binance public archive (`data.binance.vision`) import into the Parquet lake (M2-T1).

Pure modules (no network library): `paths`, `checksum`, `csvparse`, `qa`. `download` needs httpx
(imported lazily); `importer` orchestrates downloads, REST fill-ins, validation, QA and lake writes.
"""
