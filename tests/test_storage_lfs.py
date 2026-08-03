"""TDD contract for Git-LFS tracking, objects, budgets, and adapter behavior."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import StorageError, StorageIntent
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
SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DECISION_SHA256 = "86209a043bf3571d183ea7c65e24bcc45f5e0f4db15042773b282273c96c264a"
DOCUMENT_ID = "rki-176904-12345-v2"
_TRACKING = (
    "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
    "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
    "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n"
)


def config() -> LfsConfig:
    return LfsConfig(
        artifact_root="rki/Bulletins",
        max_run_objects=2,
        max_run_bytes=20,
        warn_total_bytes=30,
        block_total_bytes=40,
    )


def repository_with_tracking(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".gitattributes").write_text(_TRACKING, encoding="utf-8")
    return repository


def tree_snapshot(root: Path) -> tuple[tuple[str, bool, bytes], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.is_dir(),
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )


def materialized_pdf(
    tmp_path: Path,
    adapter: LfsStorageAdapter,
    *,
    name: str = "source.pdf",
    payload: bytes = b"%PDF-1.4\n%%EOF\n",
):
    source = tmp_path / name
    source.write_bytes(payload)
    intent = StorageIntent.from_path(
        source,
        artifact_id=f"artifact-{name}",
        logical_key=f"Jahre/1994/{name}",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    temp_root = tmp_path / f"temp-{name}"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    return intent, prepared


def test_gitattributes_tracks_only_canonical_archive_classes() -> None:
    rules = validate_lfs_tracking(ROOT / ".gitattributes")
    assert rules == (
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text",
        "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text",
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


def test_lfs_adapter_materializes_then_applies_without_git_commit(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-pdf",
        logical_key="Jahre/1994/source.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    materialize_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )
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


def test_lfs_apply_rejects_divergent_existing_target_without_overwrite(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )
    _intent, prepared = materialized_pdf(tmp_path, adapter)
    target = repository / "rki/Bulletins/Jahre/1994/source.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing archive content")

    with pytest.raises(LfsIntegrityError, match="anderen Inhalt"):
        adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))

    assert target.read_bytes() == b"existing archive content"


def test_lfs_apply_uses_shared_ledger_for_per_run_object_budget(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    tight = LfsConfig(
        artifact_root="rki/Bulletins",
        max_run_objects=1,
        max_run_bytes=10_000,
        warn_total_bytes=20_000,
        block_total_bytes=30_000,
    )
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=tight,
        authorizer=storage_rights.authorizer,
    )
    _first_intent, first = materialized_pdf(tmp_path, adapter, name="first.pdf")
    _second_intent, second = materialized_pdf(tmp_path, adapter, name="second.pdf")
    ledger = EffectLedger(RunMode.APPLY)

    adapter.apply(first, ledger=ledger)
    with pytest.raises(LfsBudgetError, match="Laufobjekte"):
        adapter.apply(second, ledger=ledger)

    assert not (repository / "rki/Bulletins/Jahre/1994/second.pdf").exists()


def test_lfs_reference_rejects_symlink_even_when_link_is_inside_repository(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"external payload")
    link = repository / "rki/Bulletins/Jahre/1994/link.pdf"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )

    with pytest.raises(LfsIntegrityError, match="Symlink|Repositoryroots"):
        adapter.reference_for_path(
            link,
            artifact_id="artifact-link",
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            document_id=DOCUMENT_ID,
            conversion_id=None,
            decision_sha256=DECISION_SHA256,
            provenance_state="current",
            visibility="repository_authorized",
            rights_state="approved",
        )


def test_lfs_adapter_verifies_pointer_against_local_object(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    oid, _object_path = write_lfs_object(repository, b"payload")
    target = repository / "rki" / "Bulletins" / "Jahre" / "1994" / "a.pdf"
    target.parent.mkdir(parents=True)
    target.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        "size 7\n",
        encoding="utf-8",
    )
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )
    reference = adapter.reference_for_path(
        target,
        artifact_id="artifact-1",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        document_id=DOCUMENT_ID,
        conversion_id=None,
        decision_sha256=DECISION_SHA256,
        provenance_state="current",
        visibility="repository_authorized",
        rights_state="approved",
    )
    adapter.verify(reference)
    assert reference.sha256 == oid


def test_lfs_reference_inventory_excludes_noncanonical_markdown(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    artifacts = repository / "rki" / "Bulletins"
    artifacts.mkdir(parents=True)
    (artifacts / "README.md").write_text("archive notes", encoding="utf-8")
    (artifacts / "Jahre" / "1994" / "notes.md").parent.mkdir(parents=True)
    (artifacts / "Jahre" / "1994" / "notes.md").write_text("not artifact", encoding="utf-8")
    (artifacts / "Jahre" / "1994" / "Markdown" / "bulletin.md").parent.mkdir(
        parents=True
    )
    (artifacts / "Jahre" / "1994" / "Markdown" / "bulletin.md").write_text(
        "canonical artifact", encoding="utf-8"
    )
    (artifacts / "Jahre" / "1994" / "PDF" / "bulletin.pdf").parent.mkdir(parents=True)
    (artifacts / "Jahre" / "1994" / "PDF" / "bulletin.pdf").write_bytes(b"%PDF")
    (artifacts / "Jahre" / "1994" / "bundle.zip").write_bytes(b"PK")
    (artifacts / "Jahre" / "1994" / "Markdown" / "upper.MD").write_text(
        "not tracked", encoding="utf-8"
    )
    (artifacts / "Jahre" / "1994" / "PDF" / "upper.PDF").write_bytes(b"%PDF")
    (artifacts / "Jahre" / "1994" / "upper.ZIP").write_bytes(b"PK")

    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )

    references = adapter.list_references()
    assert {reference.relative_path for reference in references} == {
        "rki/Bulletins/Jahre/1994/Markdown/bulletin.md",
        "rki/Bulletins/Jahre/1994/PDF/bulletin.pdf",
        "rki/Bulletins/Jahre/1994/bundle.zip",
    }
    assert all(
        reference.provenance_state == "legacy_needs_review"
        and reference.source_id is None
        and reference.decision_sha256 is None
        for reference in references
    )


def test_lfs_authorization_precedes_temp_and_repository_writes(
    tmp_path: Path,
    storage_rights,
) -> None:
    repository = repository_with_tracking(tmp_path)
    source = tmp_path / "denied.pdf"
    source.write_bytes(b"denied")
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-denied",
        logical_key="Jahre/1994/denied.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    temp_root = tmp_path / "temp-denied"
    temp_root.mkdir()
    materialize_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )
    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.materialize(intent, temp_root=temp_root, ledger=materialize_ledger)

    assert tuple(temp_root.rglob("*")) == ()
    assert materialize_ledger.events == []

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    prepared = adapter.materialize(
        intent,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    apply_ledger = EffectLedger(RunMode.APPLY)

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=apply_ledger)

    assert not (repository / "rki").exists()
    assert apply_ledger.events == []

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    target = repository / reference.relative_path
    before = target.read_bytes()
    denied_ledger = EffectLedger(RunMode.APPLY)

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=denied_ledger)

    assert target.read_bytes() == before
    assert denied_ledger.events == []


def test_lfs_constructor_rejects_structural_authorizer(
    tmp_path: Path,
    storage_rights,
) -> None:
    class ArbitraryAuthorizer:
        def authorize(self, subject: object, *, operation: str) -> None:
            pass

    class DerivedAuthorizer(type(storage_rights.authorizer)):
        pass

    repository = repository_with_tracking(tmp_path)
    invalid_authorizers = (
        ArbitraryAuthorizer(),
        DerivedAuthorizer(
            authority=storage_rights.authorizer.authority,
            policy=storage_rights.authorizer.policy,
        ),
    )
    for authorizer in invalid_authorizers:
        with pytest.raises(StorageError, match="RightsStorageAuthorizer"):
            LfsStorageAdapter(
                repository_root=repository,
                config=config(),
                authorizer=authorizer,
            )


@pytest.mark.parametrize("replacement", (b"changed", None))
def test_lfs_materialize_validates_one_source_snapshot_before_temp_write(
    tmp_path: Path,
    storage_rights,
    replacement: bytes | None,
) -> None:
    repository = repository_with_tracking(tmp_path)
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=config(),
        authorizer=storage_rights.authorizer,
    )
    source = tmp_path / "snapshot.pdf"
    source.write_bytes(b"original")
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-snapshot",
        logical_key="Jahre/1994/snapshot.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    if replacement is None:
        source.unlink()
        replacement_path = tmp_path / "replacement.pdf"
        replacement_path.write_bytes(b"original")
        source.symlink_to(replacement_path)
    else:
        source.write_bytes(replacement)
    temp_root = tmp_path / "snapshot-temp"
    temp_root.mkdir()
    sentinel = temp_root / "sentinel"
    sentinel.write_bytes(b"keep")
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    before = tree_snapshot(temp_root)

    with pytest.raises(StorageError, match="Größe|SHA-256|reguläre Datei"):
        adapter.materialize(intent, temp_root=temp_root, ledger=ledger)

    assert tree_snapshot(temp_root) == before
    assert ledger.events == []
