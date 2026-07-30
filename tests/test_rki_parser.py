"""Pure parser regressions for the modular RKI grabber."""
from __future__ import annotations

from pathlib import Path

from scripts.rki_grabber.models import AffectedPeriods, Scope
from scripts.rki_grabber.parser import (
    extract_submission_item_links,
    extract_year_collections,
    parse_item_metadata,
    parse_listing_bounds,
    target_relative_path,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rki-html"
BASE_URL = "https://edoc.rki.de"


def read(name: str) -> str:
    """Read a UTF-8 parser fixture."""

    return (FIXTURES / name).read_text(encoding="utf-8")


def test_year_and_listing_parsers_keep_only_same_origin_numeric_handles() -> None:
    years = extract_year_collections(read("p03-root.html"), base_url=BASE_URL)
    assert years == {
        1995: ("176904/1995", "https://edoc.rki.de/handle/176904/1995"),
        1996: ("176904/1996", "https://edoc.rki.de/handle/176904/1996"),
    }
    links = extract_submission_item_links(
        read("p03-listing.html"),
        current_url=f"{BASE_URL}/handle/176904/1996/recent-submissions",
        base_url=BASE_URL,
        excluded_handles={"176904/1996"},
    )
    assert links == [
        ("176904/12345.2", "https://edoc.rki.de/handle/176904/12345.2")
    ]
    assert parse_listing_bounds("Now showing items 1-1 of 1") == (1, 1, 1)


def test_item_parser_preserves_version_date_rights_and_deduplicates_bitstream() -> None:
    metadata = parse_item_metadata(
        read("p03-item.html"),
        scope=Scope.ISSUES,
        item_handle="176904/12345.2",
        item_url="https://edoc.rki.de/handle/176904/12345.2",
        fallback_year=1996,
        base_url=BASE_URL,
        response_headers={"ETag": '"item-v2"', "Last-Modified": "Fri, 22 Mar 1996 12:00:00 GMT"},
    )
    assert metadata.document_id == "rki-176904-12345-v2"
    assert metadata.source_id == "rki:176904/12345.2"
    assert metadata.version == 2
    assert metadata.publication_date == "1996-03-22"
    assert metadata.year == 1996
    assert metadata.doi == "10.25646/12345.2"
    assert metadata.rights.label == "Synthetic fixture — no publication decision"
    assert metadata.etag == '"item-v2"'
    assert len(metadata.pdfs) == 1
    candidate = metadata.pdfs[0]
    assert candidate.expected_md5 == "397039b5b63ce567c48e787bbb3e18ae"
    assert candidate.url.endswith("minimal.pdf?sequence=2")
    path = target_relative_path(metadata, candidate)
    assert path.startswith("issues/1996/")
    assert path.endswith("minimal.pdf")


def test_affected_periods_use_source_publication_date() -> None:
    periods = AffectedPeriods()
    periods.add("1996-03-22", 1996)
    assert periods.to_dict() == {
        "weeks": ["1996-W12"],
        "months": ["1996-03"],
        "years": [1996],
    }
