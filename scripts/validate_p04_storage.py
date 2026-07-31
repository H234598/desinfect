#!/usr/bin/env python3
"""Validate P04 run modes, storage contracts, LFS rules, and offline boundaries."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.run_modes import (  # noqa: E402
    EffectKind,
    EffectLedger,
    ModeViolation,
    RunMode,
)
from scripts.rki_pipeline.schema_registry import validate_document  # noqa: E402
from scripts.rki_pipeline.storage.base import StorageBackend, StorageReference  # noqa: E402
from scripts.rki_pipeline.storage.config import load_storage_config  # noqa: E402
from scripts.rki_pipeline.storage.lfs import (  # noqa: E402
    LfsBudget,
    LfsBudgetError,
    LfsInventory,
    check_lfs_budget,
    parse_lfs_pointer,
    validate_lfs_tracking,
    verify_lfs_object,
)
from scripts.rki_pipeline.storage_cli import build_parser  # noqa: E402

FORBIDDEN_NETWORK_IMPORTS = {
    "aiohttp",
    "boto3",
    "botocore",
    "ftplib",
    "github",
    "httpx",
    "requests",
    "smtplib",
    "socket",
    "urllib",
    "urllib3",
}
REQUIRED_TESTS = {
    "tests/test_run_modes.py",
    "tests/test_storage_contract.py",
    "tests/test_storage_lfs.py",
    "tests/test_storage_remote.py",
    "tests/test_storage_migration.py",
    "tests/test_storage_cli.py",
    "tests/test_validate_p04_storage.py",
}


def _runtime_paths() -> tuple[Path, ...]:
    storage_root = ROOT / "scripts" / "rki_pipeline" / "storage"
    paths = list(sorted(storage_root.glob("*.py")))
    paths.extend(
        (
            ROOT / "scripts" / "rki_pipeline" / "storage_cli.py",
            ROOT / "scripts" / "rki_pipeline" / "run_modes.py",
        )
    )
    return tuple(paths)


def _import_roots(tree: ast.AST) -> tuple[tuple[int, str, str | None], ...]:
    imports: list[tuple[int, str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                (node.lineno, alias.name.split(".", 1)[0], alias.asname)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module.split(".", 1)[0], None))
    return tuple(imports)


def _validate_git_only_subprocess(path: Path, tree: ast.AST) -> None:
    for line, root, alias in _import_roots(tree):
        if root == "subprocess" and (alias is not None or path.name != "run_modes.py"):
            raise ValueError(
                f"{path}:{line}: subprocess ist nur unverfälscht für lokale Git-Snapshots erlaubt"
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            if node.func.attr != "run" or not node.args:
                raise ValueError(f"{path}:{node.lineno}: nur subprocess.run für git ist erlaubt")
            command = node.args[0]
            if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
                raise ValueError(f"{path}:{node.lineno}: Git-Befehl muss eine literale Argumentliste sein")
            first = command.elts[0]
            if not isinstance(first, ast.Constant) or first.value != "git":
                raise ValueError(f"{path}:{node.lineno}: subprocess darf ausschließlich git starten")
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr in {"system", "popen", "spawnl", "spawnlp", "spawnv", "spawnvp"}
        ):
            raise ValueError(f"{path}:{node.lineno}: direkte Prozessstarts sind in P04 verboten")


def _validate_no_network_sdks() -> None:
    for path in _runtime_paths():
        if not path.is_file():
            raise ValueError(f"P04-Runtimemodul fehlt: {path}")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = sorted(
            {
                root
                for _line, root, _alias in _import_roots(tree)
                if root in FORBIDDEN_NETWORK_IMPORTS
            }
        )
        if forbidden:
            raise ValueError(
                f"{path}: direkte Netzwerk-SDK-/API-Imports verboten: {forbidden}"
            )
        _validate_git_only_subprocess(path, tree)


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
    plan_destinations = {
        action.dest
        for action in subparsers[0].choices["plan"]._actions  # noqa: SLF001
    }
    if "output" in plan_destinations:
        raise ValueError("RunMode plan darf keinen Dateiausgabeschalter besitzen")


def _validate_behavioral_contracts() -> None:
    plan_ledger = EffectLedger(RunMode.PLAN)
    try:
        plan_ledger.record(EffectKind.TEMP_FILE, "/tmp/forbidden")
    except ModeViolation:
        pass
    else:
        raise ValueError("RunMode plan erlaubt unerwartet einen Seiteneffekt")

    with TemporaryDirectory(prefix="desinfect-p04-validator-") as temporary:
        temp_root = Path(temporary)
        target = temp_root / "artifact.bin"
        target.write_bytes(b"payload")
        materialize = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
        materialize.record(
            EffectKind.TEMP_FILE,
            target.as_posix(),
            sha256=hashlib.sha256(b"payload").hexdigest(),
            size=7,
        )
        if materialize.events[-1].kind is not EffectKind.TEMP_FILE:
            raise ValueError("RunMode materialize registriert Temp-Artefakt nicht")

        repository = temp_root / "repository"
        oid = hashlib.sha256(b"object").hexdigest()
        object_path = repository / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(b"object")
        verify_lfs_object(repository, oid=oid, size=6)

    pointer = parse_lfs_pointer(
        "version https://git-lfs.github.com/spec/v1\n"
        + "oid sha256:"
        + "a" * 64
        + "\nsize 1\n"
    )
    if pointer.oid != "a" * 64 or pointer.size != 1:
        raise ValueError("Git-LFS-Pointervertrag ist nicht stabil")
    try:
        parse_lfs_pointer("oid sha256:" + "a" * 64 + "\nsize 1\n")
    except StorageError:
        pass
    else:
        raise ValueError("Ungültiger Git-LFS-Pointer wurde akzeptiert")

    budget = LfsBudget(
        max_run_objects=1,
        max_run_bytes=10,
        warn_total_bytes=20,
        block_total_bytes=30,
    )
    try:
        check_lfs_budget(
            budget,
            run=LfsInventory(objects=2, bytes=10),
            total=LfsInventory(objects=2, bytes=10),
        )
    except LfsBudgetError:
        pass
    else:
        raise ValueError("Git-LFS-Laufbudget blockiert Überschreitung nicht")


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
    _validate_behavioral_contracts()


if __name__ == "__main__":
    validate()
    print("P04 storage: ok; modes=plan|materialize|apply; backends=lfs|release|object; ADR-003=A; ADR-014=B")