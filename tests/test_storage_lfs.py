"""TDD contract for Git-LFS tracking, objects, budgets, and adapter behavior."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import StorageIntent
from scripts.rki_pipeline.storage.config import LfsConfig
from scripts.rki_pipeline.storage.lfs import (
    LfsBudget,
    LfsBudgetError,
    LfsIntegrityError,
    LfsInventory,
    LfsStorageAdapter,
    check_lfs_budget,
    lfs_object_path,
    parse_lfs_pointer,
    validate_lfs_tracking,
    verify_lfs_object,
)

ROOT = Path(__file__).resolve().parents[1]


def config() -> LfsConfig:
    return LfsConfig(
        artifact_root="rki/Bulletins",
        max_run_objects=2,
        max_run_bytes=20,
        warn_total_bytes=30,
        block_total_bytes=40,
    )


def test_gitattributes_tracks_only_canonical_archive_classes() -> None:
    rules = validate_lfs_tracking(ROOT / ".gitattributes")
    assert rules == (
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text",
        "rki/Bulletins/Quellen/**/*.md filter=lfs diff=lfs merge=lfs -text",
        "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text",
    )


def test_lfs_pointer_parser_is_exact() -> None:
    oid = "a" * 64
    pointer = parse_lfs_pointer(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        "size 12\n"
    )
    assert pointer.oid == oid
    assert pointer.size == 12
    with pytest.raises(LfsIntegrityError):
        parse_lfs_pointer(f"oid sha256:{oid}\nsize 12\n")
    with pytest.raises(LfsIntegrityError):
        parse_lfs_pointer(
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:BAD\nsize -1\n"
        )


def write_lfs_object(repository: Path, payload: bytes) -> tuple[str, Path]:
    oid = hashlib.sha256(payload).hexdigest()
    path = repository / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return oid, path


def test_verify_lfs_object_checks_presence_size_and_hash(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    oid, path = write_lfs_object(tmp_path, b"payload")
    verify_lfs_object(tmp_path, oid=oid, size=7)
    path.write_bytes(b"tamper!")
    with pytest.raises(LfsIntegrityError, match="SHA-256"):
        verify_lfs_object(tmp_path, oid=oid, size=7)
    path.unlink()
    with pytest.raises(LfsIntegrityError, match="fehlt"):
        verify_lfs_object(tmp_path, oid=oid, size=7)


def test_lfs_budget_enforces_run_and_total_thresholds() -> None:
    budget = LfsBudget.from_config(config())
    check_lfs_budget(
        budget,
        run=LfsInventory(objects=2, bytes=20),
        total=LfsInventory(objects=4, bytes=30),
    )
    with pytest.raises(LfsBudgetError, match="Laufobjekte"):
        check_lfs_budget(
            budget,
            run=LfsInventory(objects=3, bytes=20),
            total=LfsInventory(objects=4, bytes=30),
        )
    with pytest.raises(LfsBudgetError, match="Laufbytes"):
        check_lfs_budget(
            budget,
            run=LfsInventory(objects=2, bytes=21),
            total=LfsInventory(objects=4, bytes=30),
        )
    with pytest.warns(RuntimeWarning, match="Warnschwelle"):
        check_lfs_budget(
            budget,
            run=LfsInventory(objects=1, bytes=1),
            total=LfsInventory(objects=4, bytes=31),
        )
    with pytest.raises(LfsBudgetError, match="Blockschwelle"):
        check_lfs_budget(
            budget,
            run=LfsInventory(objects=1, bytes=1),
            total=LfsInventory(objects=5, bytes=41),
        )


def test_lfs_adapter_materializes_then_applies_without_git_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".gitattributes").write_text(
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/Quellen/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-pdf",
        logical_key="Jahre/1994/source.pdf",
        visibility="repository_authorized",
        rights_state="approved",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    materialize_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    adapter = LfsStorageAdapter(repository_root=repository, config=config())
    prepared = adapter.materialize(intent, temp_root=temp_root, ledger=materialize_ledger)
    assert prepared.path.read_bytes() == source.read_bytes()
    assert materialize_ledger.events[-1].kind is EffectKind.TEMP_FILE
    assert not (repository / "rki").exists()

    apply_ledger = EffectLedger(RunMode.APPLY)
    reference = adapter.apply(prepared, ledger=apply_ledger)
    target = repository / reference.relative_path
    pointer = parse_lfs_pointer(target.read_text(encoding="utf-8"))
    assert pointer.oid == intent.sha256
    assert pointer.size == intent.size
    object_path = lfs_object_path(repository, pointer.oid)
    assert object_path.read_bytes() == source.read_bytes()
    assert {event.kind for event in apply_ledger.events} >= {
        EffectKind.REPOSITORY_FILE,
        EffectKind.LFS,
    }
    assert not any(event.kind is EffectKind.GIT_COMMIT for event in apply_ledger.events)
    adapter.verify(reference)

    before = sorted(path.as_posix() for path in (repository / ".git" / "lfs" / "objects").rglob("*"))
    repeated = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    after = sorted(path.as_posix() for path in (repository / ".git" / "lfs" / "objects").rglob("*"))
    assert repeated == reference
    assert after == before


def test_lfs_adapter_verifies_pointer_against_local_object(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / ".gitattributes").write_text(
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/Quellen/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    oid, _object_path = write_lfs_object(repository, b"payload")
    target = repository / "rki" / "Bulletins" / "Jahre" / "1994" / "a.pdf"
    target.parent.mkdir(parents=True)
    target.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        "size 7\n",
        encoding="utf-8",
    )
    adapter = LfsStorageAdapter(repository_root=repository, config=config())
    reference = adapter.reference_for_path(
        target,
        artifact_id="artifact-1",
        visibility="repository_authorized",
        rights_state="approved",
    )
    adapter.verify(reference)
    assert reference.sha256 == oid
