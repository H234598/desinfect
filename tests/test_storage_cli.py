"""Regression tests for storage CLI mode and manifest boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.storage.base import PreparedObject
from scripts.rki_pipeline import storage_cli
from scripts.rki_pipeline.storage_cli import (
    _plan,
    _prepared_objects,
    _reference,
    build_parser,
    main,
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
    assert _prepared_objects({"objects": [payload]}) == (prepared,)


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
