#!/usr/bin/env python3
"""Validate the complete P02 schema family and its offline examples."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.schema_registry import (  # noqa: E402
    SchemaContractError,
    load_registry,
    load_schema,
    migrate_document,
    validate_document,
)


def validate() -> None:
    registry = load_registry()
    contracts = registry.get("contracts", [])
    names = [entry.get("name") for entry in contracts]
    if registry.get("schema_version") != "1.0.0" or len(contracts) != 13:
        raise SchemaContractError("Schema-Registry muss exakt dreizehn Verträge enthalten")
    if len(set(names)) != len(names) or any(not isinstance(name, str) for name in names):
        raise SchemaContractError("Schema-Registry enthält doppelte/ungültige Namen")
    for entry in contracts:
        schema = load_schema(entry["name"], registry)
        for ref in _refs(schema):
            if not ref.startswith("#/"):
                raise SchemaContractError(f"{entry['name']}: externe $ref ist in P02 unzulässig: {ref}")
    validate_document("status", json.loads((ROOT / "status.json").read_text(encoding="utf-8")))
    validate_document(
        "period-archive-manifest",
        json.loads(
            (ROOT / "tests" / "fixtures" / "schemas" / "period-archive-manifest.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    predecessors = (
        ("status", "status-v2.json"),
        ("source-manifest", "source-manifest-v1.0.json"),
        ("source-manifest", "source-manifest-v1.1.json"),
        ("document-manifest", "document-manifest-v1.0.json"),
        ("conversion-manifest", "conversion-manifest-v1.0.json"),
        ("storage-reference", "storage-reference-v1.0.json"),
    )
    for name, fixture_name in predecessors:
        predecessor = json.loads(
            (ROOT / "tests" / "fixtures" / "schemas" / fixture_name).read_text(encoding="utf-8")
        )
        first = migrate_document(name, predecessor)
        second = migrate_document(name, predecessor)
        if first != second:
            raise SchemaContractError(f"{name}-Migration ist nicht deterministisch")
        validate_document(name, first)


def _refs(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                yield child
            yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


if __name__ == "__main__":
    validate()
    print(
        "schema family: ok; 13 contracts; Draft 2020-12; "
        "status 2.0.0 -> 3.0.0; source-manifest 1.0.0/1.1.0 -> 1.2.0; "
        "document-manifest 1.0.0 -> 1.1.0; conversion-manifest 1.0.0 -> 1.1.0; "
        "storage-reference 1.0.0 -> 1.1.0"
    )
