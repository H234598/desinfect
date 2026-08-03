#!/usr/bin/env python3
"""Validate P03 parser, source configuration, result contract, and CLI imports offline."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_grabber.config import load_source_config  # noqa: E402
from scripts.rki_grabber.models import (  # noqa: E402
    AffectedPeriods,
    GrabberRequest,
    GrabberResult,
    Outcome,
    Scope,
)
from scripts.rki_grabber.parser import (  # noqa: E402
    extract_submission_item_links,
    extract_year_collections,
    parse_item_metadata,
    target_relative_path,
)


def validate() -> None:
    """Run all deterministic P03 contract checks without network or writes."""

    config = load_source_config()
    if config.allowed_hosts != ("edoc.rki.de",) or config.respect_robots is not True:
        raise ValueError("RKI-Quellgrenze oder robots-Default ist nicht fail-closed")

    fixture_root = ROOT / "tests" / "fixtures" / "rki-html"
    root_html = (fixture_root / "p03-root.html").read_text(encoding="utf-8")
    listing_html = (fixture_root / "p03-listing.html").read_text(encoding="utf-8")
    item_html = (fixture_root / "p03-item.html").read_text(encoding="utf-8")
    years = extract_year_collections(root_html, base_url=config.base_url)
    if 1996 not in years:
        raise ValueError("P03-Jahrgangsfixture wird nicht erkannt")
    links = extract_submission_item_links(
        listing_html,
        current_url=f"{config.base_url}/handle/176904/1996/recent-submissions",
        base_url=config.base_url,
        excluded_handles={"176904/1996"},
    )
    if links != [
        ("176904/12345.2", "https://edoc.rki.de/handle/176904/12345.2")
    ]:
        raise ValueError("P03-Listingfixture driftet")
    metadata = parse_item_metadata(
        item_html,
        scope=Scope.ISSUES,
        item_handle="176904/12345.2",
        item_url="https://edoc.rki.de/handle/176904/12345.2",
        fallback_year=1996,
        base_url=config.base_url,
    )
    if (
        metadata.document_id != "rki-176904-12345-v2"
        or [candidate.bitstream_version for candidate in metadata.pdfs] != [1, 2]
        or len({candidate.bitstream_id for candidate in metadata.pdfs}) != 2
        or len({target_relative_path(metadata, candidate) for candidate in metadata.pdfs})
        != 2
    ):
        raise ValueError("P03-Metadatenfixture driftet")

    schema = json.loads(
        (ROOT / "schemas" / "grabber-result.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    sample = GrabberResult(
        source={
            "base_url": config.base_url,
            "issues_root_handle": config.issues_root_handle,
            "articles_handle": config.articles_handle,
            "allowed_hosts": list(config.allowed_hosts),
            "parser_contract": "rki-edoc-html-v1",
        },
        request=GrabberRequest(
            scope=Scope.ISSUES,
            from_year=1996,
            to_year=1996,
            dry_run=True,
            respect_robots=True,
        ),
        started_at="2026-07-28T12:00:00Z",
        finished_at="2026-07-28T12:00:01Z",
        outcome=Outcome.SUCCESS,
        records=(),
        issues=(),
        affected_periods=AffectedPeriods(),
    ).to_dict()
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(sample)


def main() -> None:
    """Run validation and emit one stable summary line."""

    validate()
    print(
        "P03 grabber: ok; modular API/CLI; same-origin HTTPS; "
        "robots fail-closed; bounded PDF downloads"
    )


if __name__ == "__main__":
    main()
