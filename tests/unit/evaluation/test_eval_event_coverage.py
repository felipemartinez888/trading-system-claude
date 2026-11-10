"""Every event type of the contract has a deliberate place in the evaluation export.

A new ``EventType`` must be mapped to an export file (``scorer.FILES``) or listed below as not
exported, with the reason. Without this, a type added to the contract silently breaks the export
of any paper batch that contains it: the exporter refuses unmapped types inside a batch. The M5-3
agent events did exactly that before export format v2. Each file schema must also accept exactly
the record types its file holds.
"""

from __future__ import annotations

from typing import Any

from tsclaude.contracts.events import EventType
from tsclaude.evaluation.scorer import FILE_OF, FILES, SchemaSet

#: event type -> why it is not part of the evaluation export (counted in ``not_exported``)
NOT_EXPORTED = {
    "holdout_unlocked": "research governance (holdout guard), not a paper-session record",
}


def test_every_event_type_is_exported_or_deliberately_not() -> None:
    types = {str(t) for t in EventType}
    unplaced = sorted(types - set(FILE_OF) - set(NOT_EXPORTED))
    assert not unplaced, f"map these to an export file or list them in NOT_EXPORTED: {unplaced}"
    assert not set(FILE_OF) & set(NOT_EXPORTED)
    assert set(FILE_OF) <= types, "an export file names a type the contract does not define"
    assert set(NOT_EXPORTED) <= types, "NOT_EXPORTED names a type the contract does not define"


def _record_types(schema: dict[str, Any]) -> set[str]:
    for part in schema.get("allOf", ()):
        enum = part.get("properties", {}).get("record_type", {}).get("enum")
        if enum is not None:
            return set(enum)
    raise AssertionError(f"no record_type enum in {schema.get('$id')}")


def test_each_file_schema_accepts_exactly_its_record_types() -> None:
    schemas = SchemaSet()
    for name, types in FILES.items():
        schema = schemas.schemas[name.replace(".jsonl", ".schema.json")]
        assert _record_types(schema) == set(types), name
