
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.schema_registry import SchemaContractError, migrate_document, validate_document

ROOT = Path(__file__).resolve().parents[1]


def test_status_v2_to_v3_is_deterministic_and_keeps_dimensions_separate() -> None:
    source = json.loads(
        (ROOT / "tests" / "fixtures" / "schemas" / "status-v2.json").read_text(encoding="utf-8")
    )
    first = migrate_document("status", source)
    second = migrate_document("status", deepcopy(source))
    assert first == second
    assert first["schema_version"] == "3.0.0"
    assert first["pipeline"]["last_main_commit_at"] is None
    assert first["pipeline"]["last_successful_run_at"] == "2026-07-20T04:31:12Z"
    assert first["pipeline"]["last_successful_write_at"] == "2026-07-19T04:31:12Z"
    assert first["corpus"]["analysis_corpus_complete_through_year"] == 2020
    assert first["corpus"]["public_mirror_complete_through_year"] is None
    validate_document("status", first)


def test_only_one_predecessor_version_is_accepted() -> None:
    with pytest.raises(SchemaContractError, match="weder aktuell noch"):
        migrate_document("status", {"schema_version": "1.0.0"})
