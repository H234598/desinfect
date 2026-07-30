"""Regression tests for storage CLI mode boundaries."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rki_pipeline.storage_cli import build_parser, main


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
    plan.write_text(
        '{"schema_version":"1.0.0","source_backend":"lfs",'
        '"target_backend":"lfs","entries":[]}\n',
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".gitattributes").write_text(
        "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text\n"
        "rki/Bulletins/Quellen/**/*.md filter=lfs diff=lfs merge=lfs -text\n"
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

    from scripts.rki_pipeline.storage_cli import _reference

    payload = {
        "artifact_id": "artifact-1",
        "relative_path": "rki/Bulletins/a.pdf",
        "storage_backend": "lfs",
        "storage_object_id": "sha256:" + "a" * 64,
        "sha256": "a" * 64,
        "bytes": True,
        "visibility": "repository_authorized",
        "rights_state": "approved",
        "public_reference": None,
    }
    with pytest.raises(ValueError, match="bytes"):
        _reference(payload)
