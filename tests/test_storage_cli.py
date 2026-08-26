"""Regression tests for storage CLI mode and manifest boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageReference,
)
from scripts.rki_pipeline import storage_cli
from scripts.rki_pipeline.storage_cli import (
    _plan,
    _prepared_objects,
    _reference,
    build_parser,
    main,
)
from scripts.rki_pipeline.storage.migrate import (
    MigrationEntry,
    MigrationPlan,
    MigrationState,
)

SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DOCUMENT_ID = "rki-176904-12345-v2"
_TRACKING = (
    "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
    "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
    "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n"
)


def reference_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "artifact_id": "artifact-1",
        "relative_path": "rki/Bulletins/a.pdf",
        "storage_backend": "lfs",
        "storage_object_id": "sha256:" + "a" * 64,
        "sha256": "a" * 64,
        "bytes": 1,
        "visibility": "repository_authorized",
        "rights_state": "approved",
        "public_reference": None,
    }
    payload.update(overrides)
    return payload


def empty_plan_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "source_backend": "lfs",
        "target_backend": "lfs",
        "entries": [],
    }


def test_plan_has_no_file_output_argument() -> None:
    """RunMode plan must emit stdout only and offer no write-capable switch."""

    parser = build_parser()
    plan_parser = next(
        action.choices["plan"]
        for action in parser._actions  # noqa: SLF001 - contract inspection
        if action.__class__.__name__ == "_SubParsersAction"
    )
    destinations = {action.dest for action in plan_parser._actions}  # noqa: SLF001
    assert "output" not in destinations


def test_materialize_manifest_must_stay_below_temp_root(tmp_path: Path) -> None:
    """Even the prepared manifest is a materialize effect and cannot escape."""

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(empty_plan_payload()) + "\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".gitattributes").write_text(
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    outside = tmp_path / "outside.json"
    result = main(
        [
            "--config",
            str(Path("config/storage.toml")),
            "materialize",
            "--plan",
            str(plan),
            "--source-repo",
            str(source),
            "--temp-root",
            str(temp_root),
            "--output",
            str(outside),
        ]
    )
    assert result == 2
    assert not outside.exists()


def test_materialize_emits_prepared_manifest_version_1_1(tmp_path: Path) -> None:
    """Emitting the old shape could silently drop nullable provenance links."""

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(empty_plan_payload()) + "\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".gitattributes").write_text(
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    output = temp_root / "prepared.json"

    result = main(
        [
            "--config",
            str(Path("config/storage.toml")),
            "materialize",
            "--plan",
            str(plan),
            "--source-repo",
            str(source),
            "--temp-root",
            str(temp_root),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == "1.1.0"


def test_json_integer_fields_reject_boolean_coercion() -> None:
    """Machine inputs must not silently coerce booleans into byte counts."""

    with pytest.raises(ValueError, match="bytes"):
        _reference(reference_payload(bytes=True))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_id", None),
        ("relative_path", 42),
        ("storage_backend", None),
        ("storage_object_id", []),
        ("sha256", None),
        ("bytes", None),
        ("visibility", True),
        ("rights_state", {}),
        ("public_reference", 7),
    ),
)
def test_reference_rejects_missing_or_wrongly_typed_fields(field: str, value: object) -> None:
    payload = reference_payload(**{field: value})
    with pytest.raises(ValueError, match=field):
        _reference(payload)


def test_reference_rejects_missing_required_field() -> None:
    payload = reference_payload()
    del payload["artifact_id"]
    with pytest.raises(ValueError, match="artifact_id"):
        _reference(payload)


def test_reference_reader_migrates_legacy_before_type_construction() -> None:
    reference = _reference(reference_payload())

    assert reference.provenance_state == "legacy_needs_review"
    assert reference.source_id is None
    assert reference.source_sha256 is None
    assert reference.decision_sha256 is None
    assert _reference(reference.to_dict()) == reference


def test_reference_reader_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="Version"):
        _reference(reference_payload(schema_version="0.9.0"))


def test_prepared_object_provenance_roundtrips_without_loss(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    path = temp_root / "payload.pdf"
    path.write_bytes(b"payload")
    prepared = PreparedObject(
        artifact_id="artifact-1",
        logical_key="Jahre/1994/payload.pdf",
        path=path,
        temp_root=temp_root,
        sha256=hashlib.sha256(b"payload").hexdigest(),
        size=7,
        source_id="rki:176904/12345.2",
        source_sha256="b" * 64,
        decision_sha256="c" * 64,
        document_id="rki-176904-12345-v2",
        conversion_id="conv-" + "d" * 64,
        visibility="repository_authorized",
        rights_state="approved",
    )

    payload = storage_cli._prepared_object_payload(prepared)

    assert payload["source_id"] == "rki:176904/12345.2"
    assert payload["source_sha256"] == "b" * 64
    assert payload["decision_sha256"] == "c" * 64
    assert payload["document_id"] == "rki-176904-12345-v2"
    assert payload["conversion_id"] == "conv-" + "d" * 64
    assert _prepared_objects({"schema_version": "1.1.0", "objects": [payload]}) == (
        prepared,
    )


@pytest.mark.parametrize("version", (None, "1.0.0", "2.0.0"))
def test_prepared_reader_rejects_missing_legacy_or_unknown_version(
    version: str | None,
) -> None:
    """Prepared payloads without the complete 1.1 provenance shape must not construct."""

    payload: dict[str, object] = {"objects": []}
    if version is not None:
        payload["schema_version"] = version

    with pytest.raises(ValueError, match="Prepared-Manifest-Version"):
        _prepared_objects(payload)


def test_plan_rejects_duplicate_artifact_ids() -> None:
    source = reference_payload()
    payload = {
        "schema_version": "1.0.0",
        "source_backend": "lfs",
        "target_backend": "lfs",
        "entries": [
            {"artifact_id": "artifact-1", "state": "copy", "source": source, "target": None},
            {"artifact_id": "artifact-1", "state": "copy", "source": source, "target": None},
        ],
    }
    with pytest.raises(ValueError, match="Doppelte|artifact_id"):
        _plan(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"source_backend": None, "target_backend": "lfs", "entries": []},
        {"source_backend": "lfs", "target_backend": 3, "entries": []},
        {"source_backend": "lfs", "target_backend": "lfs", "entries": [None]},
        {
            "source_backend": "lfs",
            "target_backend": "lfs",
            "entries": [{"artifact_id": None, "state": "copy", "source": {}, "target": None}],
        },
    ),
)
def test_plan_rejects_malformed_fields_as_value_error(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _plan(payload)


def test_cli_reports_corrupt_plan_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    corrupt = tmp_path / "plan.json"
    corrupt.write_text('{"source_backend":null,"target_backend":"lfs","entries":[]}', encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    output = temp_root / "prepared.json"

    result = main(
        [
            "materialize",
            "--plan",
            str(corrupt),
            "--source-repo",
            str(source),
            "--temp-root",
            str(temp_root),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert "storage-cli:" in capsys.readouterr().err
    assert not output.exists()


def write_rights_register(tmp_path: Path, state: str) -> tuple[Path, str]:
    path = tmp_path / "rights-register.yml"
    approved = state == "approved"
    attribution = (
        "    attribution:\n"
        "      creators: [Synthetic Creator]\n"
        "      attribution_parties: [Synthetic Rights Holder]\n"
        "      copyright_notice: Synthetic copyright notice\n"
        "      license_notice: CC BY 4.0\n"
        "      license_url: https://creativecommons.org/licenses/by/4.0/\n"
        "      disclaimer_notice: Synthetic fixture only\n"
        "      origin_url: https://edoc.rki.de/handle/176904/12345.2\n"
        "      prior_change_history: []\n"
        "      current_change_notice: Unchanged synthetic fixture\n"
        if approved else "    attribution: null\n"
    )
    path.write_text(
        "schema_version: 2\n"
        "decisions:\n"
        f'  - source_id: "{SOURCE_ID}"\n'
        '    canonical_url: "https://edoc.rki.de/bitstream/handle/176904/12345.2/source.pdf?sequence=1"\n'
        f'    version_or_bitstream: "{rights.bitstream_identity("https://edoc.rki.de/bitstream/handle/176904/12345.2/source.pdf?sequence=1").bitstream_id}"\n'
        f'    source_sha256: "{SOURCE_SHA256}"\n'
        f'    state: "{state}"\n'
        f'    mode: "{"materialized" if approved else "remove_all"}"\n'
        f'    allowed_actions: {"[cache, extract_text, fetch, hash, index_text, ocr, publish, thumbnail]" if approved else "[]"}\n'
        f'    components_state: "{"cleared" if approved else "blocked"}"\n'
        + attribution
        + '    basis: "Synthetic fixture; no external publication rights claim"\n'
        '    reviewed_by: "Test Fixture"\n'
        '    reviewed_at: "2026-08-03T08:00:00Z"\n',
        encoding="utf-8",
    )
    decision_sha256 = rights.load_rights_register(path).entries[0].decision_sha256
    assert decision_sha256 is not None
    return path, decision_sha256


def migration_fixture(
    tmp_path: Path,
    *,
    decision_sha256: str,
) -> tuple[Path, Path, Path, Path, MigrationPlan]:
    payload = b"archive"
    source = tmp_path / "source-current"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".gitattributes").write_text(_TRACKING, encoding="utf-8")
    relative = "rki/Bulletins/Jahre/1994/archive.zip"
    artifact = source / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)
    reference = StorageReference(
        artifact_id="artifact-1",
        relative_path=relative,
        storage_backend=StorageBackend.LFS,
        storage_object_id="sha256:" + hashlib.sha256(payload).hexdigest(),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        document_id=DOCUMENT_ID,
        conversion_id=None,
        decision_sha256=decision_sha256,
        provenance_state="current",
        visibility="repository_authorized",
        rights_state="approved",
        public_reference=None,
    )
    migration = MigrationPlan(
        StorageBackend.LFS,
        StorageBackend.LFS,
        (MigrationEntry("artifact-1", MigrationState.COPY, reference, None),),
    )
    plan = tmp_path / "current-plan.json"
    plan.write_text(json.dumps(migration.to_dict()), encoding="utf-8")
    temp_root = tmp_path / "current-temp"
    temp_root.mkdir()
    prepared = temp_root / "prepared.json"
    target = tmp_path / "target-current"
    target.mkdir()
    (target / ".git").mkdir()
    (target / ".gitattributes").write_text(_TRACKING, encoding="utf-8")
    return plan, source, temp_root, prepared, migration


def materialize_current_cli(
    plan: Path,
    source: Path,
    temp_root: Path,
    prepared: Path,
) -> int:
    return main(
        [
            "materialize",
            "--plan",
            str(plan),
            "--source-repo",
            str(source),
            "--temp-root",
            str(temp_root),
            "--output",
            str(prepared),
        ]
    )


def test_cli_current_provenance_roundtrip_is_authorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register, decision_sha256 = write_rights_register(tmp_path, "approved")
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register)
    plan, source, temp_root, prepared, _migration = migration_fixture(
        tmp_path,
        decision_sha256=decision_sha256,
    )
    target = tmp_path / "target-current"

    assert materialize_current_cli(plan, source, temp_root, prepared) == 0
    assert main(
        [
            "apply",
            "--plan",
            str(plan),
            "--prepared",
            str(prepared),
            "--target-repo",
            str(target),
            "--confirm-apply",
        ]
    ) == 0
    assert (target / "rki/Bulletins/Jahre/1994/archive.zip").is_file()


def test_cli_revocation_between_materialize_and_apply_blocks_before_lfs_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register, decision_sha256 = write_rights_register(tmp_path, "approved")
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register)
    plan, source, temp_root, prepared, _migration = migration_fixture(
        tmp_path,
        decision_sha256=decision_sha256,
    )
    target = tmp_path / "target-current"
    assert materialize_current_cli(plan, source, temp_root, prepared) == 0
    write_rights_register(tmp_path, "takedown")

    assert main(
        [
            "apply",
            "--plan",
            str(plan),
            "--prepared",
            str(prepared),
            "--target-repo",
            str(target),
            "--confirm-apply",
        ]
    ) == 2
    assert not (target / "rki").exists()


def test_cli_legacy_plan_is_readable_but_materialize_blocks_before_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-legacy"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".gitattributes").write_text(_TRACKING, encoding="utf-8")
    payload = b"legacy"
    artifact = source / "rki/Bulletins/a.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)
    legacy = _reference(
        reference_payload(
            relative_path="rki/Bulletins/a.pdf",
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
        )
    )
    migration = MigrationPlan(
        StorageBackend.LFS,
        StorageBackend.LFS,
        (MigrationEntry("artifact-1", MigrationState.COPY, legacy, None),),
    )
    plan = tmp_path / "legacy-plan.json"
    plan.write_text(json.dumps(migration.to_dict()), encoding="utf-8")
    temp_root = tmp_path / "legacy-temp"
    temp_root.mkdir()
    prepared = temp_root / "prepared.json"

    assert materialize_current_cli(plan, source, temp_root, prepared) == 2
    assert not prepared.exists()
    assert tuple(temp_root.rglob("*")) == ()


def test_cli_requires_confirmation_before_adapter_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = tmp_path / "empty-plan.json"
    plan.write_text(json.dumps(empty_plan_payload()), encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def unexpected_adapter(config_path: Path, repository_root: Path):
        calls.append((config_path, repository_root))
        raise AssertionError("adapter must not be built")

    monkeypatch.setattr(storage_cli, "_lfs_adapter", unexpected_adapter)

    assert main(
        [
            "apply",
            "--plan",
            str(plan),
            "--prepared",
            str(tmp_path / "missing-prepared.json"),
            "--target-repo",
            str(tmp_path),
        ]
    ) == 2
    assert calls == []
