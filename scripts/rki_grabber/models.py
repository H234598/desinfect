#!/usr/bin/env python3
"""Typed, serializable contracts for the modular RKI bulletin grabber."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from scripts.rki_pipeline.documents import bitstream_identity, document_identity
from scripts.rki_pipeline.io_utils import normalize_posix_path

SCHEMA_VERSION = "1.0.0"
CANONICAL_RKI_BASE_URL = "https://edoc.rki.de"
CANONICAL_RKI_HOSTS = ("edoc.rki.de",)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_HANDLE_RE = re.compile(
    r"^(?P<prefix>[0-9]+)/(?P<number>[0-9]+)(?:\.(?P<version>[0-9]+))?$"
)


class Scope(StrEnum):
    """Supported RKI source scopes."""

    ISSUES = "issues"
    ARTICLES = "articles"
    ALL = "all"


class Outcome(StrEnum):
    """Top-level grabber outcomes and their stable CLI meaning."""

    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class RecordState(StrEnum):
    """Per-document or per-PDF processing state."""

    PLANNED = "planned"
    EXISTING = "existing"
    DOWNLOADED = "downloaded"
    RESUMED = "resumed"
    NO_PDF = "no_pdf"
    ERROR = "error"


def _finite_number(value: object, name: str) -> float:
    """Return one finite non-boolean number or raise a stable API error."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} muss eine endliche Zahl sein")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} muss eine endliche Zahl sein")
    return converted


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Validated, fixed RKI endpoint and bounded network policy."""

    base_url: str = CANONICAL_RKI_BASE_URL
    issues_root_handle: str = "176904/10"
    articles_handle: str = "176904/45"
    allowed_hosts: tuple[str, ...] = CANONICAL_RKI_HOSTS
    user_agent: str = "RKI-EpidBull-Research-Downloader/2.0"
    delay_seconds: float = 1.25
    timeout_seconds: float = 60.0
    max_redirects: int = 5
    max_listing_pages: int = 10_000
    max_html_bytes: int = 4 * 1024 * 1024
    max_pdf_bytes: int = 256 * 1024 * 1024
    robots_max_bytes: int = 512 * 1024
    respect_robots: bool = True

    def __post_init__(self) -> None:
        """Reject unsafe or nonsensical source configuration."""

        if self.base_url != CANONICAL_RKI_BASE_URL:
            raise ValueError(
                f"base_url muss die kanonische RKI-Basis-URL "
                f"{CANONICAL_RKI_BASE_URL} sein"
            )
        if self.allowed_hosts != CANONICAL_RKI_HOSTS:
            raise ValueError(
                "allowed_hosts muss exakt den kanonischen RKI-Host "
                "('edoc.rki.de',) enthalten"
            )
        if type(self.issues_root_handle) is not str or not _HANDLE_RE.fullmatch(
            self.issues_root_handle
        ):
            raise ValueError("issues_root_handle muss ein gültiger RKI-Handle sein")
        if type(self.articles_handle) is not str or not _HANDLE_RE.fullmatch(
            self.articles_handle
        ):
            raise ValueError("articles_handle muss ein gültiger RKI-Handle sein")
        if type(self.user_agent) is not str or not self.user_agent.strip():
            raise ValueError("user_agent muss eine nichtleere Zeichenkette sein")
        delay = _finite_number(self.delay_seconds, "delay_seconds")
        timeout = _finite_number(self.timeout_seconds, "timeout_seconds")
        if delay < 0 or timeout <= 0:
            raise ValueError(
                "Delay darf nicht negativ und Timeout muss positiv sein"
            )
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 10:
            raise ValueError(
                "max_redirects liegt außerhalb des sicheren Bereichs"
            )
        if (
            type(self.max_listing_pages) is not int
            or not 1 <= self.max_listing_pages <= 100_000
        ):
            raise ValueError(
                "max_listing_pages liegt außerhalb des sicheren Bereichs"
            )
        for name in (
            "max_html_bytes",
            "max_pdf_bytes",
            "robots_max_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} muss eine positive Ganzzahl sein")
        if type(self.respect_robots) is not bool:
            raise ValueError("respect_robots muss ein Boolean sein")


@dataclass(frozen=True, slots=True)
class RightsMetadata:
    """Raw source rights metadata; P06 will make the publication decision."""

    label: str | None = None
    uri: str | None = None
    copyright_notice: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Return stable JSON data without inventing a rights decision."""

        return {
            "label": self.label,
            "uri": self.uri,
            "copyright_notice": self.copyright_notice,
        }


@dataclass(frozen=True, slots=True)
class PdfCandidate:
    """One same-origin PDF bitstream advertised by an RKI item page."""

    url: str
    source_name: str
    expected_md5: str | None = None

    def __post_init__(self) -> None:
        """Validate repository checksum syntax when provided."""

        if self.expected_md5 is not None and not _MD5_RE.fullmatch(
            self.expected_md5
        ):
            raise ValueError(
                "expected_md5 muss ein kleingeschriebener MD5-Wert sein"
            )

    def to_dict(self) -> dict[str, str | None]:
        """Return stable JSON data."""

        return {
            "url": self.url,
            "source_name": self.source_name,
            "expected_md5": self.expected_md5,
        }

    @property
    def bitstream_id(self) -> str:
        """Return stable canonical identity for this PDF bitstream."""

        return bitstream_identity(self.url).bitstream_id

    @property
    def bitstream_version(self) -> int | None:
        """Return optional DSpace bitstream sequence number."""

        return bitstream_identity(self.url).version


@dataclass(frozen=True, slots=True)
class ItemMetadata:
    """Pure parser result for one RKI item page."""

    scope: Scope
    item_handle: str
    item_url: str
    title: str
    publication_date: str | None
    year: int | None
    doi: str | None
    pdfs: tuple[PdfCandidate, ...]
    rights: RightsMetadata = field(default_factory=RightsMetadata)
    etag: str | None = None
    last_modified: str | None = None

    @property
    def source_id(self) -> str:
        """Return the stable source identifier including the handle version."""

        return document_identity(self.item_handle).source_id

    @property
    def document_id(self) -> str:
        """Return a portable versioned document identifier."""

        return document_identity(self.item_handle).document_id

    @property
    def version(self) -> int:
        """Return the numeric version suffix, defaulting unversioned handles to one."""

        return document_identity(self.item_handle).version


@dataclass(frozen=True, slots=True)
class GrabberRequest:
    """Stable API request shared by CLI, pipeline, and later backfill."""

    scope: Scope = Scope.ISSUES
    from_year: int = 1994
    to_year: int = field(default_factory=lambda: date.today().year)
    dry_run: bool = False
    max_items: int | None = None
    force: bool = False
    output_root: Path = Path("rki-epidbull")
    result_path: Path | None = None
    contact: str | None = None
    user_agent: str | None = None
    delay_seconds: float | None = None
    timeout_seconds: float | None = None
    respect_robots: bool | None = None
    max_html_bytes: int | None = None
    max_pdf_bytes: int | None = None

    def __post_init__(self) -> None:
        """Validate API boundaries without touching disk or network."""

        if not isinstance(self.scope, Scope):
            raise ValueError("scope muss ein Scope-Wert sein")
        if type(self.from_year) is not int or type(self.to_year) is not int:
            raise ValueError("Jahresgrenzen müssen Ganzzahlen sein")
        if (
            not 1900 <= self.from_year <= 9999
            or not 1900 <= self.to_year <= 9999
        ):
            raise ValueError(
                "Jahresgrenzen müssen zwischen 1900 und 9999 liegen"
            )
        if self.from_year > self.to_year:
            raise ValueError(
                "from_year darf nicht größer als to_year sein"
            )
        if type(self.dry_run) is not bool or type(self.force) is not bool:
            raise ValueError("dry_run und force müssen Boolean-Werte sein")
        if self.max_items is not None:
            if type(self.max_items) is not int or self.max_items < 1:
                raise ValueError("max_items muss eine positive Ganzzahl sein")
        if self.respect_robots is not None and type(self.respect_robots) is not bool:
            raise ValueError("respect_robots muss Boolean oder None sein")
        if self.delay_seconds is not None:
            delay = _finite_number(self.delay_seconds, "delay_seconds")
            if delay < 0:
                raise ValueError("delay_seconds darf nicht negativ sein")
        if self.timeout_seconds is not None:
            timeout = _finite_number(
                self.timeout_seconds,
                "timeout_seconds",
            )
            if timeout <= 0:
                raise ValueError("timeout_seconds muss positiv sein")
        for name in ("max_html_bytes", "max_pdf_bytes"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int or value <= 0
            ):
                raise ValueError(
                    f"{name} muss eine positive Ganzzahl sein"
                )
        if not isinstance(self.output_root, Path):
            raise ValueError("output_root muss ein pathlib.Path sein")
        if self.result_path is not None and not isinstance(
            self.result_path,
            Path,
        ):
            raise ValueError("result_path muss ein pathlib.Path oder None sein")
        for name in ("contact", "user_agent"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise ValueError(f"{name} muss eine Zeichenkette oder None sein")

    def to_public_dict(self) -> dict[str, Any]:
        """Return result-safe request metadata without contact information or paths."""

        return {
            "scope": self.scope.value,
            "from_year": self.from_year,
            "to_year": self.to_year,
            "dry_run": self.dry_run,
            "max_items": self.max_items,
            "force": self.force,
            "respect_robots": self.respect_robots,
        }


@dataclass(frozen=True, slots=True)
class GrabberIssue:
    """Redacted, machine-stable error or blocker."""

    code: str
    message: str
    stage: str
    retryable: bool
    item_url: str | None = None
    pdf_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return stable JSON data."""

        return {
            "code": self.code,
            "message": self.message[:1000],
            "stage": self.stage,
            "retryable": self.retryable,
            "item_url": self.item_url,
            "pdf_url": self.pdf_url,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Structured per-PDF or no-PDF result record."""

    scope: Scope
    document_id: str
    source_id: str
    version: int
    item_handle: str
    item_url: str
    title: str
    publication_date: str | None
    year: int | None
    doi: str | None
    rights: RightsMetadata
    pdf_url: str | None
    source_filename: str | None
    relative_path: str | None
    state: RecordState
    bytes: int | None = None
    md5: str | None = None
    sha256: str | None = None
    expected_md5: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Validate hashes and durable relative paths."""

        if self.relative_path is not None:
            normalize_posix_path(PurePosixPath(self.relative_path))
        if self.sha256 is not None and not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 hat kein gültiges Format")
        for value, name in (
            (self.md5, "md5"),
            (self.expected_md5, "expected_md5"),
        ):
            if value is not None and not _MD5_RE.fullmatch(value):
                raise ValueError(f"{name} hat kein gültiges Format")
        if self.bytes is not None and self.bytes < 0:
            raise ValueError("bytes darf nicht negativ sein")

    def to_dict(self) -> dict[str, Any]:
        """Return stable JSON data."""

        return {
            "scope": self.scope.value,
            "document_id": self.document_id,
            "source_id": self.source_id,
            "version": self.version,
            "item_handle": self.item_handle,
            "item_url": self.item_url,
            "title": self.title,
            "publication_date": self.publication_date,
            "year": self.year,
            "doi": self.doi,
            "rights": self.rights.to_dict(),
            "pdf_url": self.pdf_url,
            "source_filename": self.source_filename,
            "relative_path": self.relative_path,
            "state": self.state.value,
            "bytes": self.bytes,
            "md5": self.md5,
            "sha256": self.sha256,
            "expected_md5": self.expected_md5,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "error_code": self.error_code,
            "error_message": (
                None
                if self.error_message is None
                else self.error_message[:1000]
            ),
        }


@dataclass(slots=True)
class AffectedPeriods:
    """Deterministic set of periods touched by returned document versions."""

    weeks: set[str] = field(default_factory=set)
    months: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)

    def add(
        self,
        publication_date: str | None,
        year: int | None,
    ) -> None:
        """Add exact ISO periods when a full date exists and at least the year otherwise."""

        if publication_date is not None:
            parsed = date.fromisoformat(publication_date)
            iso = parsed.isocalendar()
            self.weeks.add(f"{iso.year:04d}-W{iso.week:02d}")
            self.months.add(f"{parsed.year:04d}-{parsed.month:02d}")
            self.years.add(parsed.year)
        elif year is not None:
            self.years.add(year)

    def to_dict(self) -> dict[str, list[Any]]:
        """Return stable sorted periods."""

        return {
            "weeks": sorted(self.weeks),
            "months": sorted(self.months),
            "years": sorted(self.years),
        }


@dataclass(frozen=True, slots=True)
class GrabberResult:
    """Complete structured result returned by API and emitted by the CLI."""

    source: Mapping[str, Any]
    request: GrabberRequest
    started_at: str
    finished_at: str
    outcome: Outcome
    records: tuple[ArtifactRecord, ...]
    issues: tuple[GrabberIssue, ...]
    affected_periods: AffectedPeriods

    @property
    def exit_code(self) -> int:
        """Map outcomes to the stable CLI contract."""

        return {
            Outcome.SUCCESS: 0,
            Outcome.PARTIAL: 2,
            Outcome.BLOCKED: 3,
            Outcome.FAILED: 4,
        }[self.outcome]

    def summary(self) -> dict[str, int | bool]:
        """Calculate deterministic, non-overlapping counters."""

        states = [record.state for record in self.records]
        return {
            "records": len(self.records),
            "planned": states.count(RecordState.PLANNED),
            "existing": states.count(RecordState.EXISTING),
            "downloaded": states.count(RecordState.DOWNLOADED),
            "resumed": states.count(RecordState.RESUMED),
            "without_pdf": states.count(RecordState.NO_PDF),
            "record_errors": states.count(RecordState.ERROR),
            "issues": len(self.issues),
            "downloads_occurred": any(
                state in {
                    RecordState.DOWNLOADED,
                    RecordState.RESUMED,
                }
                for state in states
            ),
            "content_changed": any(
                state in {
                    RecordState.DOWNLOADED,
                    RecordState.RESUMED,
                }
                for state in states
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible result representation."""

        records = sorted(
            (record.to_dict() for record in self.records),
            key=lambda row: (
                row["item_url"],
                row["pdf_url"] or "",
                row["relative_path"] or "",
            ),
        )
        issues = sorted(
            (issue.to_dict() for issue in self.issues),
            key=lambda row: (
                row["stage"],
                row["code"],
                row["item_url"] or "",
                row["pdf_url"] or "",
            ),
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "source": dict(self.source),
            "request": self.request.to_public_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome.value,
            "summary": self.summary(),
            "affected_periods": self.affected_periods.to_dict(),
            "records": records,
            "issues": issues,
        }


def utc_now() -> str:
    """Return a canonical UTC timestamp with second precision."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def result_outcome(
    records: Iterable[ArtifactRecord],
    issues: Iterable[GrabberIssue],
    *,
    blocked: bool,
) -> Outcome:
    """Derive one outcome from accumulated records and issues."""

    materialized_records = tuple(records)
    materialized_issues = tuple(issues)
    if blocked:
        return Outcome.BLOCKED
    if materialized_issues:
        successful_records = tuple(
            record
            for record in materialized_records
            if record.state is not RecordState.ERROR
        )
        return (
            Outcome.PARTIAL
            if successful_records
            else Outcome.FAILED
        )
    return Outcome.SUCCESS
