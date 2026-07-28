#!/usr/bin/env python3
"""Validate the public status and the strict three-clock contract."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.schema_registry import validate_document  # noqa: E402


def validate() -> None:
    payload = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    validate_document("status", payload)
    pipeline = payload["pipeline"]
    required = {"last_main_commit_at", "last_successful_run_at", "last_successful_write_at"}
    if not required.issubset(pipeline):
        raise ValueError("status.json trennt die drei Uhren nicht vollständig")
    corpus = payload["corpus"]
    if "analysis_corpus_complete_through_year" not in corpus or "public_mirror_complete_through_year" not in corpus:
        raise ValueError("ADR-014=B verlangt getrennte Analyse- und Spiegelvollständigkeit")


if __name__ == "__main__":
    validate()
    print("public status: ok; three clocks separated; ADR-014=B")
