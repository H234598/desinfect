"""Stable RKI document and bitstream identity contracts."""
from __future__ import annotations

import pytest

from scripts.rki_pipeline.documents import (
    BitstreamIdentity,
    DocumentIdentity,
    DocumentIdentityError,
    bitstream_identity,
    document_identity,
)


def test_document_identity_uses_handle_version_not_title() -> None:
    """Changing title cannot change stable handle identity."""

    first = document_identity("176904/12345.2")
    second = document_identity("176904/12345.2")
    assert first == second == DocumentIdentity(
        handle="176904/12345.2",
        source_id="rki:176904/12345.2",
        document_id="rki-176904-12345-v2",
        version=2,
        supersedes="rki-176904-12345-v1",
    )


def test_unversioned_handle_is_explicit_version_one() -> None:
    """Omitting handle suffix must not leave version identity ambiguous."""

    identity = document_identity("176904/12345")
    assert identity.document_id == "rki-176904-12345-v1"
    assert identity.version == 1
    assert identity.supersedes is None


@pytest.mark.parametrize(
    "handle",
    (
        "176904/12345.0",
        "176904/12345.00",
        "176904/12345.01",
        "176904/12345.1",
        "999999/12345",
    ),
)
def test_document_identity_rejects_noncanonical_explicit_versions(handle: str) -> None:
    """Only unversioned RKI handles may represent version one."""

    with pytest.raises(DocumentIdentityError):
        document_identity(handle)


def test_bitstream_identity_canonicalizes_access_flag_without_losing_sequence() -> None:
    """Access-only query flag must not affect bitstream identity."""

    identity = bitstream_identity(
        "https://EDOC.RKI.DE/bitstream/handle/176904/12345.2/file.pdf"
        "?isAllowed=y&sequence=2"
    )
    assert identity.canonical_url == (
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/file.pdf?sequence=2"
    )
    assert identity.version == 2
    assert identity.bitstream_id == (
        "rki-bitstream-"
        "34798e932d2d24e4be04a2fb7c7797c7391bf238c266f86c435cc06f83e4d231"
    )


@pytest.mark.parametrize(
    "url",
    (
        "http://edoc.rki.de/bitstream/handle/176904/12345/file.pdf",
        "https://example.invalid/bitstream/handle/176904/12345/file.pdf",
        "https://edoc.rki.de:443/bitstream/handle/176904/12345/file.pdf",
        "https://user@edoc.rki.de/bitstream/handle/176904/12345/file.pdf",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.pdf#page=1",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.pdf?sequence=1&sequence=2",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.pdf?sequence=0",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.pdf?sequence=one",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.pdf?unknown=value",
        "https://edoc.rki.de/bitstream/handle/176904/12345.1/file.pdf",
        "https://edoc.rki.de/bitstream/handle/176904/12345/file.txt",
    ),
)
def test_bitstream_identity_rejects_ambiguous_or_non_pdf_urls(url: str) -> None:
    """Unsafe URL branch must never produce a stable identity."""

    with pytest.raises(DocumentIdentityError):
        bitstream_identity(url)
