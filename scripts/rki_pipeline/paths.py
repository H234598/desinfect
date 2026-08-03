#!/usr/bin/env python3
"""Canonical, portable repository paths for RKI documents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
import re
import unicodedata

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    detect_path_collisions,
    normalize_posix_path,
    relative_path_beneath,
)
from scripts.rki_pipeline.storage.config import CANONICAL_ARTIFACT_ROOT

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_PATH_CHECK_ROOT = Path("/canonical-document-paths")


class DocumentPathError(ValueError):
    """A canonical document path cannot be derived safely."""


class DocumentType(StrEnum):
    ISSUE = "gesamtausgabe"
    ARTICLE = "einzelartikel"


@dataclass(frozen=True, slots=True)
class CanonicalDocumentPaths:
    pdf: str
    markdown: str


def _validate_identity(value: str, *, name: str) -> str:
    if type(value) is not str or not value:
        raise DocumentPathError(f"{name} muss eine nichtleere Zeichenkette sein")
    if (
        _CONTROL_CHARACTERS.search(value)
        or "/" in value
        or "\\" in value
        or value.rstrip(". ") != value
        or unicodedata.normalize("NFC", value) != value
        or value.casefold() != value
        or value.upper().split(".", 1)[0] in _WINDOWS_RESERVED
    ):
        raise DocumentPathError(f"{name} ist keine portable Identität")
    return value


def _validate_component(component: str) -> None:
    if (
        not component
        or _CONTROL_CHARACTERS.search(component)
        or component.rstrip(". ") != component
        or component.upper().split(".", 1)[0] in _WINDOWS_RESERVED
        or len(component.encode("utf-8")) > 240
    ):
        raise DocumentPathError(f"Unsichere Pfadkomponente: {component!r}")


def _validate_paths(paths: CanonicalDocumentPaths) -> CanonicalDocumentPaths:
    try:
        normalized = CanonicalDocumentPaths(
            pdf=normalize_posix_path(paths.pdf),
            markdown=normalize_posix_path(paths.markdown),
        )
        for path in (normalized.pdf, normalized.markdown):
            for component in path.split("/"):
                _validate_component(component)
            if relative_path_beneath(_PATH_CHECK_ROOT / path, _PATH_CHECK_ROOT).as_posix() != path:
                raise DocumentPathError(f"Pfad liegt nicht unter kanonischer Wurzel: {path}")
        detect_path_collisions((normalized.pdf, normalized.markdown))
    except UnsafePathError as exc:
        raise DocumentPathError(str(exc)) from exc
    return normalized


def canonical_document_paths(
    *,
    document_id: str,
    bitstream_id: str,
    document_type: DocumentType,
    publication_date: str,
) -> CanonicalDocumentPaths:
    """Return PDF and Markdown paths relative below ``rki/Bulletins``."""

    document_id = _validate_identity(document_id, name="document_id")
    bitstream_id = _validate_identity(bitstream_id, name="bitstream_id")
    if not isinstance(document_type, DocumentType):
        raise DocumentPathError("document_type ist ungültig")
    if type(publication_date) is not str:
        raise DocumentPathError("publication_date muss ISO-8601 sein")
    try:
        published = date.fromisoformat(publication_date)
    except ValueError as exc:
        raise DocumentPathError("publication_date muss ISO-8601 sein") from exc

    stem = f"{published.isoformat()}_{document_type}_{document_id}_{bitstream_id}"
    if any(len(f"{stem}{suffix}".encode("utf-8")) > 240 for suffix in (".pdf", ".md")):
        stem = (
            f"{published.isoformat()}_{document_type}_"
            f"d-{sha256(document_id.encode()).hexdigest()}_"
            f"b-{sha256(bitstream_id.encode()).hexdigest()}"
        )
    if document_type is DocumentType.ISSUE:
        directories = ("Jahre", str(published.year))
    else:
        directories = ("Einzelartikel", str(published.year), f"{published.month:02d}")
    base = "/".join((*directories,))
    return _validate_paths(
        CanonicalDocumentPaths(
            pdf=f"{base}/PDF/{stem}.pdf",
            markdown=f"{base}/Markdown/{stem}.md",
        )
    )


def repository_document_paths(
    *,
    document_id: str,
    bitstream_id: str,
    document_type: DocumentType,
    publication_date: str,
) -> CanonicalDocumentPaths:
    """Return canonical document paths below the repository artifact root."""

    paths = canonical_document_paths(
        document_id=document_id,
        bitstream_id=bitstream_id,
        document_type=document_type,
        publication_date=publication_date,
    )
    return _validate_paths(
        CanonicalDocumentPaths(
            pdf=f"{CANONICAL_ARTIFACT_ROOT}/{paths.pdf}",
            markdown=f"{CANONICAL_ARTIFACT_ROOT}/{paths.markdown}",
        )
    )
