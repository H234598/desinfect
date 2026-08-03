#!/usr/bin/env python3
"""Versioned JSON-Schema registry and deterministic one-version migrations."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from scripts.rki_pipeline.conversion.base import (
    EvidenceError,
    RuntimeEvidence,
    ToolEvidence,
    conversion_fingerprint,
    conversion_id,
)
from scripts.rki_pipeline.storage.base import validate_storage_provenance_relationship

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "config" / "schema-registry.json"


class SchemaContractError(ValueError):
    """A schema, document version, or migration violates the contract."""


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Load the strict schema registry."""

    return json.loads(path.read_text(encoding="utf-8"))


def contract(name: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one uniquely registered contract by name."""

    data = registry or load_registry()
    matches = [entry for entry in data.get("contracts", []) if entry.get("name") == name]
    if len(matches) != 1:
        raise SchemaContractError(f"Schema-Vertrag muss genau einmal registriert sein: {name}")
    return matches[0]


def load_schema(name: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load and meta-validate one registered Draft 2020-12 schema."""

    entry = contract(name, registry)
    path = ROOT / entry["path"]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaContractError(f"Schema nicht lesbar: {path}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaContractError(f"{name}: Schema ist ungültig") from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SchemaContractError(f"{name}: ausschließlich Draft 2020-12 ist zulässig")
    version = schema.get("properties", {}).get("schema_version", {}).get("const")
    if version != entry["current_version"]:
        raise SchemaContractError(f"{name}: Registry und Schema-Version driften")
    return schema


def validate_document(name: str, payload: dict[str, Any]) -> None:
    """Validate one document against the current registered contract."""

    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        rendered = "; ".join(
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in errors[:8]
        )
        raise SchemaContractError(f"{name}: {rendered}")
    if name == "source-manifest" and payload["same_content_as"] != sorted(payload["same_content_as"]):
        raise SchemaContractError("source-manifest: same_content_as muss lexikalisch sortiert sein")
    if name == "conversion-manifest" and payload["provenance_state"] == "current":
        try:
            toolchain = tuple(ToolEvidence.from_dict(item) for item in payload["toolchain"])
            runtime = RuntimeEvidence.from_dict(payload["runtime"])
            expected_fingerprint = conversion_fingerprint(
                source_sha256=payload["source_sha256"],
                options_sha256=payload["options_sha256"],
                toolchain=toolchain,
                runtime=runtime,
            )
            expected_conversion_id = conversion_id(
                payload["document_id"],
                payload["bitstream_id"],
                expected_fingerprint,
            )
        except (EvidenceError, KeyError, TypeError) as exc:
            raise SchemaContractError(f"conversion-manifest: {exc}") from exc
        if payload["fingerprint_sha256"] != expected_fingerprint:
            raise SchemaContractError("conversion-manifest: Fingerprint stimmt nicht mit Evidenz überein")
        if payload["conversion_id"] != expected_conversion_id:
            raise SchemaContractError("conversion-manifest: conversion_id stimmt nicht mit Identität überein")
    if name == "storage-reference":
        try:
            validate_storage_provenance_relationship(
                payload.get("source_id"),
                payload.get("document_id"),
            )
        except ValueError as exc:
            raise SchemaContractError(f"storage-reference: {exc}") from exc


def _nullable_year(value: Any) -> int | None:
    return value if type(value) is int and 1990 <= value <= 9999 else None


def migrate_status_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate the single supported predecessor status contract deterministically."""

    version = payload.get("schema_version")
    if version not in {2, "2", "2.0.0"}:
        raise SchemaContractError(f"Nicht unterstützte Statusmigration: {version!r}")
    source = deepcopy(payload)
    runtime = source.get("runtime") if isinstance(source.get("runtime"), dict) else {}
    pipeline = source.get("pipeline") if isinstance(source.get("pipeline"), dict) else {}
    periods = source.get("periods") if isinstance(source.get("periods"), dict) else {}
    corpus = source.get("corpus") if isinstance(source.get("corpus"), dict) else {}
    watchdog = source.get("watchdog") if isinstance(source.get("watchdog"), dict) else {}

    result = {
        "schema_version": "3.0.0",
        "project": "desinfect",
        "repository": "H234598/desinfect",
        "status": source.get("status") if source.get("status") in {
            "not_initialized", "operational", "degraded", "blocked", "unknown"
        } else "unknown",
        "updated_at": source.get("updated_at"),
        "runtime": {
            "storage_backend": runtime.get("storage_backend", "lfs"),
            "last_run_mode": runtime.get("last_run_mode", runtime.get("run_mode")),
        },
        "pipeline": {
            "last_main_commit_at": pipeline.get("last_main_commit_at"),
            "last_successful_run_at": pipeline.get("last_successful_run_at"),
            "last_successful_write_at": pipeline.get("last_successful_write_at"),
            "consecutive_failures": pipeline.get("consecutive_failures", 0),
            "last_error": pipeline.get("last_error"),
        },
        "periods": {
            "last_completed_week": periods.get("last_completed_week"),
            "last_completed_month": periods.get("last_completed_month"),
            "last_completed_year": _nullable_year(periods.get("last_completed_year")),
            "last_reconciliation_at": periods.get("last_reconciliation_at"),
            "last_recovery_drill_year": _nullable_year(periods.get("last_recovery_drill_year")),
        },
        "corpus": {
            "inventory_complete_through_year": _nullable_year(
                corpus.get("inventory_complete_through_year", corpus.get("pdf_complete_through_year"))
            ),
            "analysis_corpus_complete_through_year": _nullable_year(
                corpus.get("analysis_corpus_complete_through_year", corpus.get("markdown_complete_through_year"))
            ),
            "public_mirror_complete_through_year": _nullable_year(
                corpus.get("public_mirror_complete_through_year")
            ),
            "taxonomy_gate_satisfied": bool(corpus.get("taxonomy_gate_satisfied", False)),
            "taxonomy_state": corpus.get("taxonomy_state", "blocked"),
        },
        "watchdog": {
            "interval_days": watchdog.get("interval_days", 45),
            "last_reset_at": watchdog.get("last_reset_at"),
            "next_bark_at": watchdog.get("next_bark_at"),
            "last_bark_at": watchdog.get("last_bark_at"),
            "reset_by": watchdog.get("reset_by"),
        },
    }
    validate_document("status", result)
    return result


def migrate_source_manifest_v1_0_to_v1_1(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate exactly source-manifest 1.0.0 without inventing missing evidence."""

    if payload.get("schema_version") != "1.0.0":
        raise SchemaContractError(f"Nicht unterstützte Source-Manifest-Migration: {payload.get('schema_version')!r}")
    result = deepcopy(payload)
    result.update(
        {
            "schema_version": "1.1.0",
            "provenance_state": "legacy_needs_review",
            "bitstream_id": None,
            "bitstream_url": None,
            "bitstream_version": None,
            "rights_evidence": {
                "label": None,
                "license_url": None,
                "copyright_notice": None,
                "open_access": None,
            },
            "decision_sha256": None,
            "same_content_as": [],
        }
    )
    validate_document("source-manifest", result)
    return result


def migrate_document_manifest_v1_0_to_v1_1(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate exactly document-manifest 1.0.0 and derive calendar periods."""

    if payload.get("schema_version") != "1.0.0":
        raise SchemaContractError(f"Nicht unterstützte Document-Manifest-Migration: {payload.get('schema_version')!r}")
    result = deepcopy(payload)
    try:
        published = date.fromisoformat(result["publication_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaContractError("Document-Manifest braucht ein ISO-Publikationsdatum") from exc
    iso_year, iso_week, _ = published.isocalendar()
    result.update(
        {
            "schema_version": "1.1.0",
            "provenance_state": "legacy_needs_review",
            "bitstream_id": None,
            "bitstream_version": None,
            "canonical_periods": {
                "week": f"{iso_year:04d}-W{iso_week:02d}",
                "month": f"{published.year:04d}-{published.month:02d}",
                "year": published.year,
            },
            "superseded_by": None,
        }
    )
    validate_document("document-manifest", result)
    return result


def migrate_conversion_manifest_v1_0_to_v1_1(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate conversion-manifest 1.0.0 without inventing tool evidence."""

    if payload.get("schema_version") != "1.0.0":
        raise SchemaContractError(
            "Nicht unterstützte Conversion-Manifest-Migration: "
            f"{payload.get('schema_version')!r}"
        )
    result = deepcopy(payload)
    result.update(
        {
            "schema_version": "1.1.0",
            "conversion_id": None,
            "bitstream_id": None,
            "page_count": None,
            "toolchain": None,
            "runtime": None,
            "fingerprint_sha256": None,
            "storage_reference": None,
            "provenance_state": "legacy_needs_review",
        }
    )
    validate_document("conversion-manifest", result)
    return result


def migrate_storage_reference_v1_0_to_v1_1(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate storage-reference 1.0.0 without inventing authorization."""

    if payload.get("schema_version") != "1.0.0":
        raise SchemaContractError(
            f"Nicht unterstützte Storage-Reference-Migration: {payload.get('schema_version')!r}"
        )
    result = deepcopy(payload)
    result.update(
        {
            "schema_version": "1.1.0",
            "source_id": None,
            "source_sha256": None,
            "document_id": None,
            "conversion_id": None,
            "decision_sha256": None,
            "provenance_state": "legacy_needs_review",
        }
    )
    validate_document("storage-reference", result)
    return result


MIGRATIONS: dict[tuple[str, str, str], Callable[[dict[str, Any]], dict[str, Any]]] = {
    ("status", "2.0.0", "3.0.0"): migrate_status_v2_to_v3,
    ("source-manifest", "1.0.0", "1.1.0"): migrate_source_manifest_v1_0_to_v1_1,
    ("document-manifest", "1.0.0", "1.1.0"): migrate_document_manifest_v1_0_to_v1_1,
    ("conversion-manifest", "1.0.0", "1.1.0"): migrate_conversion_manifest_v1_0_to_v1_1,
    ("storage-reference", "1.0.0", "1.1.0"): migrate_storage_reference_v1_0_to_v1_1,
}


def migrate_document(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate exactly one registered predecessor version to the current contract."""

    entry = contract(name)
    current = entry["current_version"]
    raw_version = payload.get("schema_version")
    source_version = "2.0.0" if raw_version in {2, "2", "2.0.0"} else raw_version
    if source_version == current:
        result = deepcopy(payload)
        validate_document(name, result)
        return result
    if source_version not in entry.get("previous_versions", []):
        raise SchemaContractError(
            f"{name}: Version {source_version!r} ist weder aktuell noch die unterstützte Vorversion"
        )
    migration = MIGRATIONS.get((name, str(source_version), current))
    if migration is None:
        raise SchemaContractError(f"{name}: registrierte Migration fehlt")
    return migration(payload)
