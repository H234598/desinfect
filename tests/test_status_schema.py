
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document

ROOT = Path(__file__).resolve().parents[1]


def test_status_has_three_independent_clocks() -> None:
    payload = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    validate_document("status", payload)
    assert set(payload["pipeline"]) >= {
        "last_main_commit_at", "last_successful_run_at", "last_successful_write_at"
    }


def test_analysis_and_public_mirror_are_independent() -> None:
    payload = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    payload["corpus"]["analysis_corpus_complete_through_year"] = 2020
    payload["corpus"]["public_mirror_complete_through_year"] = None
    validate_document("status", payload)


def test_status_rejects_non_utc_timestamp() -> None:
    payload = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    payload["updated_at"] = "2026-07-28T00:00:00+02:00"
    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document("status", payload)
