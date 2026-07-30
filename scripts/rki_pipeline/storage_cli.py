#!/usr/bin/env python3
"""Offline-safe CLI for storage verification and LFS-to-LFS migration drills."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scripts.rki_pipeline.io_utils import atomic_write_text, stable_json_dumps
from scripts.rki_pipeline.run_modes import EffectLedger, RunMode
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


def _reference(payload: dict[str, object]) -> StorageReference:
    return StorageReference(
        artifact_id=str(payload["artifact_id"]),
        relative_path=str(payload["relative_path"]),
        storage_backend=StorageBackend(str(payload["storage_backend"])),
        storage_object_id=str(payload["storage_object_id"]),
        sha256=str(payload["sha256"]),
        size=int(payload["bytes"]),
        visibility=str(payload["visibility"]),
        rights_state=str(payload["rights_state"]),
        public_reference=(
            None
            if payload.get("public_reference") is None
            else str(payload["public_reference"])
        ),
    )


def _plan(payload: dict[str, object]) -> MigrationPlan:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Migrationsplan besitzt keine Eintragsliste")
    entries: list[MigrationEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict) or not isinstance(raw.get("source"), dict):
            raise ValueError("Ungültiger Migrationseintrag")
        target_payload = raw.get("target")
        entries.append(
            MigrationEntry(
                artifact_id=str(raw["artifact_id"]),
                state=MigrationState(str(raw["state"])),
                source=_reference(raw["source"]),
                target=(
                    None
                    if target_payload is None
                    else _reference(target_payload)
                ),
            )
        )
    return MigrationPlan(
        StorageBackend(str(payload["source_backend"])),
        StorageBackend(str(payload["target_backend"])),
        tuple(entries),
    )


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
    plan.add_argument("--output", type=Path)

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
            payload = {**migration.to_dict(), "sha256": migration.sha256}
            rendered = stable_json_dumps(payload)
            if args.output is None:
                print(rendered, end="")
            else:
                atomic_write_text(args.output, rendered, allowed_root=args.output.parent)
            return 0

        migration = _plan(_load_json(args.plan))
        if args.command == "materialize":
            source = _lfs_adapter(args.config, args.source_repo)
            ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=args.temp_root)
            prepared = materialize_migration(
                migration,
                source,
                temp_root=args.temp_root,
                ledger=ledger,
            )
            payload = {
                "schema_version": "1.0.0",
                "plan_sha256": migration.sha256,
                "objects": [
                    {
                        "artifact_id": item.artifact_id,
                        "logical_key": item.logical_key,
                        "path": item.path.absolute().as_posix(),
                        "temp_root": item.temp_root.absolute().as_posix(),
                        "sha256": item.sha256,
                        "size": item.size,
                        "visibility": item.visibility,
                        "rights_state": item.rights_state,
                    }
                    for item in prepared
                ],
            }
            atomic_write_text(args.output, stable_json_dumps(payload), allowed_root=args.output.parent)
            return 0

        if not args.confirm_apply:
            raise StorageError("apply benötigt --confirm-apply")
        prepared_payload = _load_json(args.prepared)
        if prepared_payload.get("plan_sha256") != migration.sha256:
            raise StorageError("Prepared-Manifest gehört zu einem anderen Migrationsplan")
        raw_objects = prepared_payload.get("objects")
        if not isinstance(raw_objects, list):
            raise ValueError("Prepared-Manifest besitzt keine Objektliste")
        prepared = tuple(
            PreparedObject(
                artifact_id=str(item["artifact_id"]),
                logical_key=str(item["logical_key"]),
                path=Path(str(item["path"])),
                temp_root=Path(str(item["temp_root"])),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                visibility=str(item["visibility"]),
                rights_state=str(item["rights_state"]),
            )
            for item in raw_objects
            if isinstance(item, dict)
        )
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
