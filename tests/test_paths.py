"""Canonical document-path contract."""
from __future__ import annotations

from hashlib import sha256

import pytest

from scripts.rki_pipeline.paths import (
    DocumentPathError,
    DocumentType,
    canonical_document_paths,
    repository_document_paths,
)


def test_issue_paths_use_publication_year_and_blueprint_markdown_directory() -> None:
    paths = canonical_document_paths(
        document_id="rki-176904-12345-v2",
        bitstream_id="rki-bitstream-" + "a" * 64,
        document_type=DocumentType.ISSUE,
        publication_date="1996-03-22",
    )
    stem = "1996-03-22_gesamtausgabe_rki-176904-12345-v2_rki-bitstream-" + "a" * 64
    assert paths.pdf == f"Jahre/1996/PDF/{stem}.pdf"
    assert paths.markdown == f"Jahre/1996/Markdown/{stem}.md"


def test_article_paths_use_publication_month() -> None:
    paths = canonical_document_paths(
        document_id="rki-176904-88-v1",
        bitstream_id="rki-bitstream-" + "b" * 64,
        document_type=DocumentType.ARTICLE,
        publication_date="2000-01-01",
    )
    assert paths.pdf.startswith("Einzelartikel/2000/01/PDF/2000-01-01_einzelartikel_")
    assert paths.markdown.startswith(
        "Einzelartikel/2000/01/Markdown/2000-01-01_einzelartikel_"
    )


@pytest.mark.parametrize(
    ("publication_date", "expected_directory"),
    [
        ("1999-12-31", "Jahre/1999/PDF/1999-12-31_gesamtausgabe_"),
        ("2000-01-01", "Jahre/2000/PDF/2000-01-01_gesamtausgabe_"),
    ],
)
def test_issue_paths_keep_publication_year_boundary(
    publication_date: str, expected_directory: str
) -> None:
    paths = canonical_document_paths(
        document_id="rki-176904-12-v1",
        bitstream_id="rki-bitstream-" + "c" * 64,
        document_type=DocumentType.ISSUE,
        publication_date=publication_date,
    )
    assert paths.pdf.startswith(expected_directory)


@pytest.mark.parametrize("publication_date", ("", "1996-02-30", "1996/03/22", None))
def test_paths_reject_missing_or_invalid_publication_date(publication_date: object) -> None:
    with pytest.raises(DocumentPathError):
        canonical_document_paths(
            document_id="rki-176904-12-v1",
            bitstream_id="rki-bitstream-" + "e" * 64,
            document_type=DocumentType.ISSUE,
            publication_date=publication_date,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("document_id", ("CON", "con", "rki-176904-12-v1. ", "bad\x00id"))
def test_paths_reject_unsafe_windows_filename_identity_components(document_id: str) -> None:
    with pytest.raises(DocumentPathError):
        canonical_document_paths(
            document_id=document_id,
            bitstream_id="rki-bitstream-" + "f" * 64,
            document_type=DocumentType.ISSUE,
            publication_date="1996-03-22",
        )


def test_paths_reject_casefold_colliding_identity() -> None:
    with pytest.raises(DocumentPathError):
        canonical_document_paths(
            document_id="RKI-176904-12-V1",
            bitstream_id="rki-bitstream-" + "a" * 64,
            document_type=DocumentType.ISSUE,
            publication_date="1996-03-22",
        )


def test_overlong_identity_uses_full_hash_tokens_and_portable_components() -> None:
    document_id = "rki-" + "d" * 300
    bitstream_id = "rki-bitstream-" + "b" * 64
    paths = canonical_document_paths(
        document_id=document_id,
        bitstream_id=bitstream_id,
        document_type=DocumentType.ISSUE,
        publication_date="1996-03-22",
    )
    expected = (
        "1996-03-22_gesamtausgabe_"
        f"d-{sha256(document_id.encode()).hexdigest()}_"
        f"b-{sha256(bitstream_id.encode()).hexdigest()}"
    )
    assert paths.pdf == f"Jahre/1996/PDF/{expected}.pdf"
    assert paths.markdown == f"Jahre/1996/Markdown/{expected}.md"
    for path in (paths.pdf, paths.markdown):
        assert all(len(component.encode("utf-8")) <= 240 for component in path.split("/"))


def test_repository_paths_prefix_canonical_artifact_root_once() -> None:
    paths = repository_document_paths(
        document_id="rki-176904-12-v1",
        bitstream_id="rki-bitstream-" + "a" * 64,
        document_type=DocumentType.ARTICLE,
        publication_date="1996-03-22",
    )
    assert paths.pdf.startswith("rki/Bulletins/Einzelartikel/1996/03/PDF/")
    assert paths.markdown.startswith("rki/Bulletins/Einzelartikel/1996/03/Markdown/")
