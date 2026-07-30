#!/usr/bin/env python3
"""Validate P04 run modes, storage contracts, LFS rules, and offline boundaries."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.schema_registry import validate_document  # noqa: E402
from scripts.rki_pipeline.storage.base import StorageBackend, StorageReference  # noqa: E402
from scripts.rki_pipeline.storage.config import load_storage_config  # noqa: E402
from scripts.rki_pipeline.storage.lfs import validate_lfs_tracking  # noqa: E402
from scripts.rki_pipeline.storage_cli import build_parser  # noqa: E402

FORBIDDEN_IMPORTS = {
    "boto3",
    "botocore",
    "github",
    "httpx",
    "requests",
}
REQUIRED_TESTS = {
    "tests/test_run_modes.py",
    "tests/test_storage_contract.py",
    "tests/test_storage_lfs.py",
    "tests/test_storage_remote.py",
    "tests/test_storage_migration.py",
}


def _validate_no_network_sdks() -> None:
    storage_root = ROOT / "scripts" / "rki_pipeline" / "storage"
    for path in sorted(storage_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = (node.module.split(".", 1)[0],)
            forbidden = sorted(set(names) & FORBIDDEN_IMPORTS)
            if forbidden:
                raise ValueError(f"{path}: direkte Netzwerk-SDK-Imports verboten: {forbidden}")


def _validate_cli() -> None:
    parser = build_parser()
    subparsers = [
        action
        for action in parser._actions  # noqa: SLF001 - validator inspects parser contract
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    if len(subparsers) != 1:
        raise ValueError("Storage-CLI benötigt genau einen Subparservertrag")
    choices = set(subparsers[0].choices)
    if choices != {"verify", "plan", "materialize", "apply"}:
        raise ValueError(f"Storage-CLI-Subcommands driften: {sorted(choices)}")


def validate() -> None:
    config = load_storage_config(ROOT / "config" / "storage.toml")
    if config.backend is not StorageBackend.LFS:
        raise ValueError("Git LFS muss initiales Storage-Backend bleiben")
    validate_lfs_tracking(ROOT / ".gitattributes")
    plan_source = json.loads((ROOT / "config" / "plan-source.json").read_text(encoding="utf-8"))
    if plan_source.get("locked_decisions") != {"ADR-003": "A", "ADR-014": "B"}:
        raise ValueError("ADR-003=A und ADR-014=B müssen gesperrt bleiben")
    reference = StorageReference(
        artifact_id="p04-validator-sample",
        relative_path="rki/Bulletins/Jahre/1994/sample.pdf",
        storage_backend=StorageBackend.LFS,
        storage_object_id="sha256:" + "a" * 64,
        sha256="a" * 64,
        size=1,
        visibility="repository_authorized",
        rights_state="approved",
        public_reference=None,
    )
    validate_document("storage-reference", reference.to_dict())
    missing = sorted(path for path in REQUIRED_TESTS if not (ROOT / path).is_file())
    if missing:
        raise ValueError(f"P04-Testdateien fehlen: {missing}")
    _validate_no_network_sdks()
    _validate_cli()


if __name__ == "__main__":
    validate()
    print("P04 storage: ok; modes=plan|materialize|apply; backends=lfs|release|object; ADR-003=A; ADR-014=B")
