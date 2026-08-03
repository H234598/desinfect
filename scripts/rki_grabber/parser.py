#!/usr/bin/env python3
"""Pure HTML and metadata parsers extracted from the original RKI grabber."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from scripts.rki_grabber.models import (
    ItemMetadata,
    PdfCandidate,
    RightsMetadata,
    Scope,
)
from scripts.rki_pipeline.paths import DocumentPathError, DocumentType, canonical_document_paths

HANDLE_PATH_RE = re.compile(
    r"^/handle/(?P<handle>176904/[0-9]+(?:\.[0-9]+)?)/?$"
)
PDF_PATH_RE = re.compile(
    r"/bitstream/handle/176904/[0-9]+(?:\.[0-9]+)?/.+\.pdf$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-([01]\d)-([0-3]\d)\b")
TOTAL_EN_RE = re.compile(
    r"Now\s+showing\s+items\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d.,]+)",
    re.IGNORECASE,
)
TOTAL_DE_RE = re.compile(
    r"(?:Einträge|Elemente)\s+(\d+)\s*[-–]\s*(\d+)\s+"
    r"(?:von|aus)\s+([\d.,]+)",
    re.IGNORECASE,
)
MD5_RE = re.compile(r"\bMD5\s*:\s*([0-9a-f]{32})\b", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.25646/[0-9]+(?:\.[0-9]+)?\b", re.IGNORECASE)


def soup_from_html(html: str) -> BeautifulSoup:
    """Parse one HTML document using BeautifulSoup's standard parser."""

    return BeautifulSoup(html, "html.parser")


def normalize_handle_url(base_url: str, url: str) -> tuple[str, str] | None:
    """Return a clean same-origin RKI handle URL or ``None``."""

    absolute = urljoin(base_url + "/", url)
    parsed = urlsplit(absolute)
    base = urlsplit(base_url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != base.netloc.lower():
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    match = HANDLE_PATH_RE.match(parsed.path)
    if match is None:
        return None
    handle = match.group("handle")
    clean = urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return handle, clean


def with_offset(url: str, offset: int) -> str:
    """Return a listing URL with one normalized DSpace ``offset`` parameter."""

    if offset < 0:
        raise ValueError("offset darf nicht negativ sein")
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if offset:
        query["offset"] = str(offset)
    else:
        query.pop("offset", None)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def parse_listing_bounds(text: str) -> tuple[int, int, int] | None:
    """Parse localized DSpace item range text."""

    for regex in (TOTAL_EN_RE, TOTAL_DE_RE):
        match = regex.search(text)
        if match is None:
            continue
        start, end, total_raw = match.groups()
        total = int(re.sub(r"\D", "", total_raw))
        values = (int(start), int(end), total)
        if values[0] < 1 or values[1] < values[0] or values[2] < values[1]:
            raise ValueError(f"Unplausible Listing-Grenzen: {values}")
        return values
    return None


def extract_year_collections(
    html: str,
    *,
    base_url: str,
) -> dict[int, tuple[str, str]]:
    """Extract the RKI year collection map from the issue root page."""

    soup = soup_from_html(html)
    result: dict[int, tuple[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        if not re.fullmatch(r"(?:19|20)\d{2}", text):
            continue
        normalized = normalize_handle_url(base_url, str(anchor["href"]))
        if normalized is not None:
            result[int(text)] = normalized
    if not result:
        raise ValueError("Keine RKI-Jahrgangssammlungen gefunden")
    return dict(sorted(result.items()))


def extract_submission_item_links(
    html: str,
    *,
    current_url: str,
    base_url: str,
    excluded_handles: set[str],
) -> list[tuple[str, str]]:
    """Extract unique item handle links from one DSpace listing page."""

    soup = soup_from_html(html)
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        normalized = normalize_handle_url(base_url, urljoin(current_url, anchor["href"]))
        if normalized is None:
            continue
        handle, url = normalized
        if handle in excluded_handles:
            continue
        text = anchor.get_text(" ", strip=True)
        if not (
            ISO_DATE_RE.search(text)
            or "Epidemiologisches Bulletin" in text
            or YEAR_RE.search(text)
        ):
            continue
        found[url] = handle
    return sorted(((handle, url) for url, handle in found.items()), key=lambda row: row[1])


def find_next_page(html: str, *, current_url: str) -> str | None:
    """Return the listing's own next-page URL when recognizable."""

    soup = soup_from_html(html)
    for anchor in soup.find_all("a", href=True):
        rel_value = anchor.get("rel", [])
        rel = " ".join(rel_value) if isinstance(rel_value, list) else str(rel_value)
        class_value = anchor.get("class", [])
        classes = (
            " ".join(class_value) if isinstance(class_value, list) else str(class_value)
        )
        text = anchor.get_text(" ", strip=True).lower()
        if (
            "next" in rel.lower()
            or "next" in classes.lower()
            or text in {"next", "weiter", ">", "›", "»"}
        ):
            return urljoin(current_url, str(anchor["href"]))
    return None


def first_meta(soup: BeautifulSoup, names: Sequence[str]) -> str | None:
    """Return the first non-empty matching ``name`` or ``property`` metadata value."""

    wanted = {name.lower() for name in names}
    for meta in soup.find_all("meta"):
        name = str(meta.get("name", "")).lower()
        prop = str(meta.get("property", "")).lower()
        if name not in wanted and prop not in wanted:
            continue
        content = str(meta.get("content", "")).strip()
        if content:
            return content
    return None


def _publication_date(soup: BeautifulSoup, page_text: str, title: str) -> str | None:
    """Return an exact ISO publication date when the item exposes one."""

    raw = first_meta(
        soup,
        (
            "dc.date",
            "dc.date.issued",
            "dcterms.issued",
            "citation_publication_date",
            "citation_date",
        ),
    )
    for candidate in (raw or "", page_text[:2000], title):
        match = ISO_DATE_RE.search(candidate)
        if match is None:
            continue
        try:
            return date.fromisoformat(match.group(0)).isoformat()
        except ValueError:
            continue
    return None


def _year(
    publication_date: str | None,
    page_text: str,
    title: str,
    fallback_year: int | None,
) -> int | None:
    """Return the best source publication year, falling back to the collection year."""

    if publication_date is not None:
        return date.fromisoformat(publication_date).year
    for candidate in (page_text[:2000], title):
        match = YEAR_RE.search(candidate)
        if match is not None:
            return int(match.group(1))
    return fallback_year


def _rights(soup: BeautifulSoup) -> RightsMetadata:
    """Preserve raw rights metadata without making a publication decision."""

    return RightsMetadata(
        label=first_meta(
            soup,
            ("dc.rights", "dcterms.rights", "citation_publication_rights"),
        ),
        uri=first_meta(
            soup,
            ("dc.rights.uri", "dcterms.license", "license"),
        ),
        copyright_notice=first_meta(
            soup,
            ("dc.rights.holder", "dcterms.rightsholder", "copyright"),
        ),
    )


def _doi(soup: BeautifulSoup, page_text: str) -> str | None:
    """Return the canonical RKI DOI when present."""

    raw = first_meta(soup, ("dc.identifier", "citation_doi", "dc.identifier.doi"))
    for candidate in (raw or "", page_text):
        match = DOI_RE.search(candidate)
        if match is not None:
            return match.group(0).lower()
    return None


def _pdf_candidates(
    soup: BeautifulSoup,
    *,
    response_url: str,
    base_url: str,
) -> tuple[PdfCandidate, ...]:
    """Extract unique same-origin PDF bitstreams and optional repository MD5 values."""

    base_host = urlsplit(base_url).netloc.lower()
    found: dict[str, PdfCandidate] = {}
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(response_url, str(anchor["href"]))
        parsed = urlsplit(absolute)
        if (
            parsed.scheme.lower() != "https"
            or parsed.netloc.lower() != base_host
            or parsed.username is not None
            or parsed.password is not None
            or PDF_PATH_RE.search(parsed.path) is None
        ):
            continue
        source_name = anchor.get_text(" ", strip=True) or Path(unquote(parsed.path)).name
        source_name = source_name.split("—", 1)[0].strip()
        if not source_name.lower().endswith(".pdf"):
            source_name = Path(unquote(parsed.path)).name
        expected_md5 = None
        parent = anchor.parent
        if isinstance(parent, Tag):
            match = MD5_RE.search(parent.get_text(" ", strip=True))
            if match is not None:
                expected_md5 = match.group(1).lower()
        candidate = PdfCandidate(
            url=urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, "")),
            source_name=unquote(source_name),
            expected_md5=expected_md5,
        )
        previous = found.get(candidate.bitstream_id)
        if (
            previous is not None
            and previous.expected_md5 is not None
            and candidate.expected_md5 is not None
            and previous.expected_md5 != candidate.expected_md5
        ):
            raise ValueError("Widersprüchliche MD5-Werte für RKI-Bitstream")
        if previous is None or (
            previous.expected_md5 is None and candidate.expected_md5 is not None
        ):
            found[candidate.bitstream_id] = candidate
    return tuple(
        sorted(
            found.values(),
            key=lambda candidate: (
                candidate.bitstream_version is None,
                candidate.bitstream_version or 0,
                candidate.bitstream_id,
            ),
        )
    )


def parse_item_metadata(
    html: str,
    *,
    scope: Scope,
    item_handle: str,
    item_url: str,
    fallback_year: int | None,
    base_url: str,
    response_headers: Mapping[str, str] | None = None,
) -> ItemMetadata:
    """Parse one item page into a typed, deterministic metadata record."""

    if HANDLE_PATH_RE.fullmatch(urlsplit(item_url).path) is None:
        raise ValueError(f"item_url ist kein gültiger RKI-Handle: {item_url}")
    soup = soup_from_html(html)
    page_text = soup.get_text(" ", strip=True)
    title = first_meta(
        soup,
        ("dc.title", "dcterms.title", "citation_title", "og:title"),
    )
    if title is None:
        heading = soup.find(["h1", "h2"])
        title = heading.get_text(" ", strip=True) if heading else item_handle
    title = re.sub(r"\s+", " ", title).strip()
    publication_date = _publication_date(soup, page_text, title)
    headers = {key.lower(): value for key, value in (response_headers or {}).items()}
    return ItemMetadata(
        scope=scope,
        item_handle=item_handle,
        item_url=item_url,
        title=title,
        publication_date=publication_date,
        year=_year(publication_date, page_text, title, fallback_year),
        doi=_doi(soup, page_text),
        pdfs=_pdf_candidates(soup, response_url=item_url, base_url=base_url),
        rights=_rights(soup),
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
    )


def safe_component(value: str, *, max_length: int = 100) -> str:
    """Return a portable legacy display component without using it as an identity."""

    if max_length < 8:
        raise ValueError("max_length ist zu klein")
    value = unquote(value)
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"[<>:\"/\\|?*]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value or "datei")[:max_length].rstrip(" ._")


def target_relative_path(metadata: ItemMetadata, candidate: PdfCandidate) -> str:
    """Return canonical PDF path beneath the caller-owned output root."""

    document_type = {
        Scope.ISSUES: DocumentType.ISSUE,
        Scope.ARTICLES: DocumentType.ARTICLE,
    }.get(metadata.scope)
    if document_type is None:
        raise DocumentPathError("Scope.ALL besitzt keinen kanonischen Dokumentpfad")
    return canonical_document_paths(
        document_id=metadata.document_id,
        bitstream_id=candidate.bitstream_id,
        document_type=document_type,
        publication_date=metadata.publication_date,
    ).pdf
