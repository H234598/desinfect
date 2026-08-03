#!/usr/bin/env python3
"""Deterministic, identity-bound Markdown frontmatter for RKI conversions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
import unicodedata

from scripts.rki_pipeline.documents import DocumentIdentityError, document_identity
from scripts.rki_pipeline.paths import DocumentType

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BULLETIN = re.compile(r"^(?:0?[1-9]|[1-4][0-9]|5[0-3])/[0-9]{4}$")
_DOI = re.compile(r"^10\.[0-9]{4,9}/\S+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_PDF = re.compile(r"^\.\./PDF/[^/\\]+\.pdf$")


def _text(value: object, *, name: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or _CONTROL.search(value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{name} ist keine kanonische Zeichenkette")
    return value


@dataclass(frozen=True, slots=True)
class MarkdownMetadata:
    """Reviewed source metadata that becomes part of converted Markdown bytes."""

    title: str
    document_type: DocumentType
    publication_date: date
    bulletin_number: str | None
    doi: str | None

    def __post_init__(self) -> None:
        _text(self.title, name="title", maximum=1_000)
        if not isinstance(self.document_type, DocumentType):
            raise ValueError("document_type ist ungültig")
        if type(self.publication_date) is not date or self.publication_date.year < 1990:
            raise ValueError("publication_date ist ungültig")
        if self.bulletin_number is not None and (
            type(self.bulletin_number) is not str
            or _BULLETIN.fullmatch(self.bulletin_number) is None
        ):
            raise ValueError("bulletin_number ist nicht kanonisch")
        if self.doi is not None and (
            type(self.doi) is not str
            or len(self.doi) > 300
            or _CONTROL.search(self.doi)
            or _DOI.fullmatch(self.doi) is None
        ):
            raise ValueError("doi ist nicht kanonisch")

    def fingerprint_dict(self) -> dict[str, object]:
        return {
            "bulletin_number": self.bulletin_number,
            "document_type": self.document_type.value,
            "doi": self.doi,
            "publication_date": self.publication_date.isoformat(),
            "title": self.title,
        }


def _scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _nullable_scalar(value: str | None) -> str:
    return "null" if value is None else _scalar(value)


def render_frontmatter(
    metadata: MarkdownMetadata,
    *,
    document_id: str,
    source_id: str,
    source_pdf: str,
    source_sha256: str,
    conversion_quality: str,
    ocr_used: bool,
) -> str:
    """Render stable YAML-compatible frontmatter with derived tags."""

    if type(metadata) is not MarkdownMetadata:
        raise TypeError("metadata muss ein exaktes MarkdownMetadata sein")
    if type(source_id) is not str or not source_id.startswith("rki:"):
        raise ValueError("source_id ist nicht kanonisch")
    rki_handle = source_id.removeprefix("rki:")
    try:
        identity = document_identity(rki_handle)
    except DocumentIdentityError as exc:  # pragma: no cover - guarded by dataclass
        raise ValueError("source_id ist nicht kanonisch") from exc
    if document_id != identity.document_id:
        raise ValueError("document_id gehört nicht zum RKI-Handle")
    if (
        type(source_pdf) is not str
        or _CONTROL.search(source_pdf)
        or _SOURCE_PDF.fullmatch(source_pdf) is None
    ):
        raise ValueError("source_pdf ist kein kanonischer relativer PDF-Pfad")
    if type(source_sha256) is not str or _SHA256.fullmatch(source_sha256) is None:
        raise ValueError("source_sha256 ist ungültig")
    if conversion_quality not in {"good", "needs_review"}:
        raise ValueError("conversion_quality ist ungültig")
    if type(ocr_used) is not bool or (ocr_used and conversion_quality != "needs_review"):
        raise ValueError("ocr_used widerspricht conversion_quality")

    published = metadata.publication_date
    lines = [
        "---",
        f"id: {_scalar(document_id)}",
        f"title: {_scalar(metadata.title)}",
        f"document_type: {_scalar(metadata.document_type.value)}",
        f"publication_date: {_scalar(published.isoformat())}",
        f"year: {published.year}",
        f"month: {published.month}",
        f"bulletin_number: {_nullable_scalar(metadata.bulletin_number)}",
        f"doi: {_nullable_scalar(metadata.doi)}",
        f"rki_handle: {_scalar(identity.handle)}",
        f"source_url: {_scalar(f'https://edoc.rki.de/handle/{identity.handle}')}",
        f"source_pdf: {_scalar(source_pdf)}",
        f"source_sha256: {_scalar(source_sha256)}",
        f"conversion_quality: {_scalar(conversion_quality)}",
        f"ocr_used: {str(ocr_used).lower()}",
        "tags:",
        '  - "rki"',
        '  - "epidemiologisches-bulletin"',
        '  - "quelle"',
        f'  - "jahr/{published.year}"',
        "---",
        "",
    ]
    return "\n".join(lines) + "\n"
