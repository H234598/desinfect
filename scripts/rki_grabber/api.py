#!/usr/bin/env python3
"""Stable public Python API and deterministic result persistence for the RKI grabber."""
from __future__ import annotations

import csv
from dataclasses import replace
import io
import json
from pathlib import Path
from typing import Callable

from scripts.rki_grabber.config import load_source_config
from scripts.rki_grabber.http import HttpTransport, PoliteClient
from scripts.rki_grabber.models import GrabberRequest, GrabberResult, SourceConfig, utc_now
from scripts.rki_grabber.service import RkiGrabberService
from scripts.rki_pipeline.io_utils import atomic_write_text, stable_json_dumps


def _effective_config(request: GrabberRequest, config: SourceConfig) -> SourceConfig:
    """Apply validated per-request network limits without mutating canonical config."""

    return replace(
        config,
        delay_seconds=(
            config.delay_seconds
            if request.delay_seconds is None
            else request.delay_seconds
        ),
        timeout_seconds=(
            config.timeout_seconds
            if request.timeout_seconds is None
            else request.timeout_seconds
        ),
        max_html_bytes=(
            config.max_html_bytes
            if request.max_html_bytes is None
            else request.max_html_bytes
        ),
        max_pdf_bytes=(
            config.max_pdf_bytes
            if request.max_pdf_bytes is None
            else request.max_pdf_bytes
        ),
        respect_robots=(
            config.respect_robots
            if request.respect_robots is None
            else request.respect_robots
        ),
        user_agent=request.user_agent or config.user_agent,
    )


def grab(
    request: GrabberRequest,
    *,
    config: SourceConfig | None = None,
    transport: HttpTransport | None = None,
    now: Callable[[], str] = utc_now,
) -> GrabberResult:
    """Run the same importable grabber used by CLI, dispatcher, and backfill."""

    canonical = config or load_source_config()
    effective = _effective_config(request, canonical)
    client = PoliteClient(
        effective,
        transport=transport,
        contact=request.contact,
        user_agent=effective.user_agent,
        delay_seconds=effective.delay_seconds,
        timeout_seconds=effective.timeout_seconds,
        respect_robots=effective.respect_robots,
    )
    return RkiGrabberService(effective, client, now=now).grab(request)


def write_result(path: Path, result: GrabberResult, *, allowed_root: Path) -> None:
    """Atomically write the canonical result JSON beneath an explicit root."""

    atomic_write_text(
        path,
        stable_json_dumps(result.to_dict()),
        allowed_root=allowed_root,
    )


def _legacy_csv(result: GrabberResult) -> str:
    """Render compatibility CSV from the canonical records without extra state."""

    rows = result.to_dict()["records"]
    fieldnames = [
        "scope",
        "year",
        "title",
        "doi",
        "item_handle",
        "item_url",
        "pdf_url",
        "source_filename",
        "relative_path",
        "state",
        "bytes",
        "md5",
        "sha256",
        "expected_md5",
        "error_code",
        "error_message",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def write_legacy_outputs(output_root: Path, result: GrabberResult) -> None:
    """Write result.json plus deterministic JSONL/CSV/run-info compatibility files."""

    payload = result.to_dict()
    write_result(output_root / "result.json", result, allowed_root=output_root)
    jsonl = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in payload["records"]
    )
    atomic_write_text(
        output_root / "manifest.jsonl",
        jsonl,
        allowed_root=output_root,
    )
    atomic_write_text(
        output_root / "manifest.csv",
        _legacy_csv(result),
        allowed_root=output_root,
    )
    atomic_write_text(
        output_root / "run-info.json",
        stable_json_dumps(
            {
                "schema_version": payload["schema_version"],
                "source": payload["source"],
                "request": payload["request"],
                "started_at": payload["started_at"],
                "finished_at": payload["finished_at"],
                "outcome": payload["outcome"],
                "summary": payload["summary"],
                "affected_periods": payload["affected_periods"],
            }
        ),
        allowed_root=output_root,
    )
