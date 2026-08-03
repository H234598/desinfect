#!/usr/bin/env python3
"""Offline-safe CLI for storage verification and LFS-to-LFS migration drills."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from scripts.rki_pipeline.io_utils import (
    atomic_write_text,
    relative_path_beneath,
    stable_json_dumps,
)
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import migrate_document
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageError,
    StorageReference,
)
from scripts.rki_pipeline.storage.config import load_storage_config
from scripts.rki_pipeline.storage.factory import build_storage_adapter
from scripts.rki_pipeline.storage.lfs import inventory_lfs_objects, validate_lfs_tracking
from scripts.rki_pipeline.storage.migrate import (
    MigrationEntry,
    MigrationPlan,
    MigrationState,
    apply_migration,
    materialize_migration,
    plan_migration,
)


def _required(payload: dict[str, object], name: str) -> object:
    if name not in payload:
        raise ValueError(f"Pflichtfeld fehlt: {name}")
    return payload[name]


def _string(payload: dict[str, object], name: str) -> str:
    value = _required(payload, name)
    if type(value) is not str or not value:
        raise ValueError(f"{name} muss eine nichtleere Zeichenkette sein")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = _required(payload, name)
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} muss eine nichtnegative Ganzzahl sein")
    return value


def _mapping(payload: dict[str, object], name: str) -> dict[str, object]:
    value = _required(payload, name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} muss ein JSON-Objekt sein")
    return value


def _backend(payload: dict[str, object], name: str) -> StorageBackend:
    raw = _string(payload, name)
    try:
        return StorageBackend(raw)
    except ValueError as exc:
        raise ValueError(f"{name} enthält ein unbekanntes Storage-Backend: {raw}") from exc


def _nullable_string(payload: dict[str, object], name: str) -> str | None:
    value = _required(payload, name)
    if value is not None and type(value) is not str:
        raise ValueError(f"{name} muss eine Zeichenkette oder null sein")
    return value


def _reference(payload: dict[str, object]) -> StorageReference:
    current = migrate_document("storage-reference", payload)
    return StorageReference(
        artifact_id=_string(current, "artifact_id"),
        relative_path=_string(current, "relative_path"),
        storage_backend=_backend(current, "storage_backend"),
        storage_object_id=_string(current, "storage_object_id"),
        sha256=_string(current, "sha256"),
        size=_integer(current, "bytes"),
        source_id=_nullable_string(current, "source_id"),
        source_sha256=_nullable_string(current, "source_sha256"),
        document_id=_nullable_string(current, "document_id"),
        conversion_id=_nullable_string(current, "conversion_id"),
        decision_sha256=_nullable_string(current, "decision_sha256"),
        provenance_state=_string(current, "provenance_state"),
        visibility=_string(current, "visibility"),
        rights_state=_string(current, "rights_state"),
        public_reference=_nullable_string(current, "public_reference"),
    )


def _plan(payload: dict[str, object]) -> MigrationPlan:
    if payload.get("schema_version") not in {None, "1.0.0"}:
        raise ValueError("Unbekannte Migrationsplan-Version")
    source_backend = _backend(payload, "source_backend")
    target_backend = _backend(payload, "target_backend")
    raw_entries = _required(payload, "entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Migrationsplan besitzt keine Eintragsliste")
    entries: list[MigrationEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"Migrationseintrag {index} muss ein JSON-Objekt sein")
        artifact_id = _string(raw, "artifact_id")
        if artifact_id in seen:
            raise ValueError(f"Doppelte artifact_id im Migrationsplan: {artifact_id}")
        seen.add(artifact_id)
        raw_state = _string(raw, "state")
        try:
            state = MigrationState(raw_state)
        except ValueError as exc:
            raise ValueError(f"Unbekannter Migrationszustand: {raw_state}") from exc
        source = _reference(_mapping(raw, "source"))
        if source.artifact_id != artifact_id:
            raise ValueError(f"source.artifact_id weicht ab: {artifact_id}")
        target_payload = raw.get("target")
        if target_payload is not None and not isinstance(target_payload, dict):
            raise ValueError("target muss ein JSON-Objekt oder null sein")
        target = None if target_payload is None else _reference(target_payload)
        if target is not None and target.artifact_id != artifact_id:
            raise ValueError(f"target.artifact_id weicht ab: {artifact_id}")
        entries.append(
            MigrationEntry(
                artifact_id=artifact_id,
                state=state,
                source=source,
                target=target,
            )
        )
    return MigrationPlan(source_backend, target_backend, tuple(entries))


def _prepared_objects(payload: dict[str, object]) -> tuple[PreparedObject, ...]:
    if payload.get("schema_version") != "1.1.0":
        raise ValueError(
            f"Unbekannte Prepared-Manifest-Version: {payload.get('schema_version')!r}"
        )
    raw_objects = _required(payload, "objects")
    if not isinstance(raw_objects, list):
        raise ValueError("Prepared-Manifest besitzt keine Objektliste")
    prepared: list[PreparedObject] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_objects):
        if not isinstance(item, dict):
            raise ValueError(f"Prepared-Objekt {index} muss ein JSON-Objekt sein")
        artifact_id = _string(item, "artifact_id")
        if artifact_id in seen:
            raise ValueError(f"Doppelte vorbereitete artifact_id: {artifact_id}")
        seen.add(artifact_id)
        prepared.append(
            PreparedObject(
                artifact_id=artifact_id,
                logical_key=_string(item, "logical_key"),
                path=Path(_string(item, "path")),
                temp_root=Path(_string(item, "temp_root")),
                sha256=_string(item, "sha256"),
                size=_integer(item, "size"),
                source_id=_string(item, "source_id"),
                source_sha256=_string(item, "source_sha256"),
                decision_sha256=_string(item, "decision_sha256"),
                document_id=_nullable_string(item, "document_id"),
                conversion_id=_nullable_string(item, "conversion_id"),
                visibility=_string(item, "visibility"),
                rights_state=_string(item, "rights_state"),
            )
        )
    return tuple(prepared)


def _prepared_object_payload(item: PreparedObject) -> dict[str, object]:
    return {
        "artifact_id": item.artifact_id,
        "logical_key": item.logical_key,
        "path": item.path.absolute().as_posix(),
        "temp_root": item.temp_root.absolute().as_posix(),
        "sha256": item.sha256,
        "size": item.size,
        "source_id": item.source_id,
        "source_sha256": item.source_sha256,
        "decision_sha256": item.decision_sha256,
        "document_id": item.document_id,
        "conversion_id": item.conversion_id,
        "visibility": item.visibility,
        "rights_state": item.rights_state,
    }


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON-Wurzel muss ein Objekt sein: {path}")
    return value


def _lfs_adapter(config_path: Path, repository_root: Path):
    config = load_storage_config(config_path)
    return build_storage_adapter(
        config,
        backend=StorageBackend.LFS,
        repository_root=repository_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/storage.toml"))
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="Verify local Git-LFS rules and object inventory")
    verify.add_argument("--repository-root", type=Path, default=Path("."))

    plan = sub.add_parser("plan", help="Create a deterministic LFS migration plan")
    plan.add_argument("--source-repo", type=Path, required=True)
    plan.add_argument("--target-repo", type=Path, required=True)

    materialize = sub.add_parser("materialize", help="Export copy entries below temp_root")
    materialize.add_argument("--plan", type=Path, required=True)
    materialize.add_argument("--source-repo", type=Path, required=True)
    materialize.add_argument("--temp-root", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)

    apply = sub.add_parser("apply", help="Publish a prepared LFS migration")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--prepared", type=Path, required=True)
    apply.add_argument("--target-repo", type=Path, required=True)
    apply.add_argument("--confirm-apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            validate_lfs_tracking(args.repository_root / ".gitattributes")
            inventory = inventory_lfs_objects(args.repository_root)
            print(stable_json_dumps({"objects": inventory.objects, "bytes": inventory.bytes}), end="")
            return 0

        if args.command == "plan":
            source = _lfs_adapter(args.config, args.source_repo)
            target = _lfs_adapter(args.config, args.target_repo)
            migration = plan_migration(source, target)
            print(stable_json_dumps({**migration.to_dict(), "sha256": migration.sha256}), end="")
            return 0

        migration = _plan(_load_json(args.plan))
        if args.command == "materialize":
            relative_path_beneath(args.output, args.temp_root)
            source = _lfs_adapter(args.config, args.source_repo)
            ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=args.temp_root)
            prepared = materialize_migration(
                migration,
                source,
                temp_root=args.temp_root,
                ledger=ledger,
            )
            payload = {
                "schema_version": "1.1.0",
                "plan_sha256": migration.sha256,
                "objects": [_prepared_object_payload(item) for item in prepared],
            }
            rendered = stable_json_dumps(payload)
            atomic_write_text(args.output, rendered, allowed_root=args.temp_root)
            encoded = rendered.encode("utf-8")
            ledger.record(
                EffectKind.TEMP_FILE,
                args.output.absolute().as_posix(),
                sha256=hashlib.sha256(encoded).hexdigest(),
                size=len(encoded),
            )
            return 0

        if not args.confirm_apply:
            raise StorageError("apply benötigt --confirm-apply")
        prepared_payload = _load_json(args.prepared)
        plan_sha256 = prepared_payload.get("plan_sha256")
        if type(plan_sha256) is not str or plan_sha256 != migration.sha256:
            raise StorageError("Prepared-Manifest gehört zu einem anderen Migrationsplan")
        prepared = _prepared_objects(prepared_payload)
        target = _lfs_adapter(args.config, args.target_repo)
        references = apply_migration(
            migration,
            prepared,
            target,
            ledger=EffectLedger(RunMode.APPLY),
        )
        print(stable_json_dumps({"references": [reference.to_dict() for reference in references]}), end="")
        return 0
    except (OSError, ValueError, StorageError, json.JSONDecodeError) as exc:
        print(f"storage-cli: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
