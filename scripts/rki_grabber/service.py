#!/usr/bin/env python3
"""Importable orchestrator that composes pure parsers, HTTP, and secure downloads."""
from __future__ import annotations

from collections.abc import Callable, Iterator
import logging
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.rki_grabber.download import PdfDownloadError, download_pdf
from scripts.rki_grabber.http import (
    GrabberHttpError,
    PoliteClient,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from scripts.rki_grabber.models import (
    AffectedPeriods,
    ArtifactRecord,
    GrabberIssue,
    GrabberRequest,
    GrabberResult,
    ItemMetadata,
    Outcome,
    RecordState,
    Scope,
    SourceConfig,
    result_outcome,
    utc_now,
)
from scripts.rki_grabber.parser import (
    extract_submission_item_links,
    extract_year_collections,
    find_next_page,
    parse_item_metadata,
    parse_listing_bounds,
    target_relative_path,
    with_offset,
)


class GrabberServiceError(RuntimeError):
    """Base class for orchestration errors that are not source transport errors."""

    code = "grabber.service"
    retryable = False


class PaginationError(GrabberServiceError):
    """A DSpace listing loops, stalls, or exceeds its bounded page count."""

    code = "grabber.pagination"
    retryable = True


class RkiGrabberService:
    """Single-threaded, resumable RKI source service used by API and CLI."""

    def __init__(
        self,
        config: SourceConfig,
        client: PoliteClient,
        *,
        now: Callable[[], str] = utc_now,
    ) -> None:
        """Bind a validated source policy and an injectable HTTP client."""

        self.config = config
        self.client = client
        self.now = now

    @property
    def source_descriptor(self) -> dict[str, Any]:
        """Return stable source metadata for every structured result."""

        return {
            "base_url": self.config.base_url,
            "issues_root_handle": self.config.issues_root_handle,
            "articles_handle": self.config.articles_handle,
            "allowed_hosts": list(self.config.allowed_hosts),
            "parser_contract": "rki-edoc-html-v1",
        }

    def _html(self, url: str) -> tuple[str, dict[str, str], str]:
        """Fetch one bounded HTML document and return text, headers, final URL."""

        payload = self.client.get_bytes(
            url,
            max_bytes=self.config.max_html_bytes,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        )
        return payload.text(), dict(payload.headers), payload.url

    def iter_submission_items(
        self,
        *,
        collection_handle: str,
        excluded_handles: set[str],
    ) -> Iterator[tuple[str, str]]:
        """Yield unique item links from bounded DSpace recent-submissions pages."""

        base_listing_url = (
            f"{self.config.base_url}/handle/{collection_handle}/recent-submissions"
        )
        page_url: str | None = base_listing_url
        seen_pages: set[str] = set()
        seen_items: set[str] = set()
        expected_total: int | None = None

        while page_url is not None:
            if len(seen_pages) >= self.config.max_listing_pages:
                raise PaginationError("Listing überschreitet max_listing_pages")
            if page_url in seen_pages:
                raise PaginationError(f"Listing-Schleife erkannt: {page_url}")
            seen_pages.add(page_url)
            html, _headers, final_url = self._html(page_url)
            page_items = extract_submission_item_links(
                html,
                current_url=final_url,
                base_url=self.config.base_url,
                excluded_handles=excluded_handles,
            )
            new_items = [
                (handle, url)
                for handle, url in page_items
                if url not in seen_items
            ]
            for handle, url in new_items:
                seen_items.add(url)
                yield handle, url

            bounds = parse_listing_bounds(
                BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            )
            if bounds is not None:
                start, end, expected_total = bounds
                logging.debug(
                    "Listing %s-%s von %s; neu=%s",
                    start,
                    end,
                    expected_total,
                    len(new_items),
                )
                if len(seen_items) >= expected_total or end >= expected_total:
                    break

            discovered_next = find_next_page(
                html,
                current_url=final_url,
            )
            if (
                discovered_next is not None
                and discovered_next not in seen_pages
            ):
                page_url = self.client.validate_url(discovered_next)
                continue
            if bounds is None:
                page_url = None
                continue
            _start, end, _total = bounds
            candidate = with_offset(base_listing_url, end)
            if (
                candidate in seen_pages
                or (not new_items and len(seen_pages) > 1)
            ):
                raise PaginationError(
                    "Pagination lieferte keine neuen Einträge; "
                    "offset-Vertrag könnte driften"
                )
            page_url = candidate

        if expected_total is not None and len(seen_items) < expected_total:
            logging.warning(
                "Listing meldet %s rohe Einträge; nach sicheren Handle- und "
                "Textfiltern bleiben %s eindeutige Dokumentlinks. "
                "Die Pagination selbst ist vollständig durchlaufen.",
                expected_total,
                len(seen_items),
            )

    def collect_scope_items(
        self,
        request: GrabberRequest,
        scope: Scope,
    ) -> Iterator[tuple[str, str, int | None]]:
        """Yield item handles, URLs, and optional collection-year fallbacks."""

        if scope is Scope.ISSUES:
            root_url = (
                f"{self.config.base_url}/handle/"
                f"{self.config.issues_root_handle}"
            )
            html, _headers, _final = self._html(root_url)
            years = extract_year_collections(
                html,
                base_url=self.config.base_url,
            )
            all_collection_handles = {
                handle for handle, _url in years.values()
            }
            excluded = {
                self.config.issues_root_handle,
                self.config.articles_handle,
                *all_collection_handles,
            }
            selected = [
                (year, data)
                for year, data in years.items()
                if request.from_year <= year <= request.to_year
            ]
            for year, (collection_handle, _collection_url) in selected:
                for item_handle, item_url in self.iter_submission_items(
                    collection_handle=collection_handle,
                    excluded_handles=excluded,
                ):
                    yield item_handle, item_url, year
            return
        if scope is Scope.ARTICLES:
            excluded = {
                self.config.issues_root_handle,
                self.config.articles_handle,
            }
            for item_handle, item_url in self.iter_submission_items(
                collection_handle=self.config.articles_handle,
                excluded_handles=excluded,
            ):
                yield item_handle, item_url, None
            return
        raise ValueError(f"Unbekannter Scope: {scope}")

    def _metadata(
        self,
        *,
        scope: Scope,
        item_handle: str,
        item_url: str,
        fallback_year: int | None,
    ) -> ItemMetadata:
        """Fetch and parse one item page."""

        html, headers, final_url = self._html(item_url)
        return parse_item_metadata(
            html,
            scope=scope,
            item_handle=item_handle,
            item_url=final_url,
            fallback_year=fallback_year,
            base_url=self.config.base_url,
            response_headers=headers,
        )

    @staticmethod
    def _issue(
        exc: BaseException,
        *,
        stage: str,
        item_url: str | None = None,
        pdf_url: str | None = None,
    ) -> GrabberIssue:
        """Map known exceptions to a redacted structured issue."""

        code = str(getattr(exc, "code", f"{stage}.error"))
        retryable = bool(getattr(exc, "retryable", False))
        message = str(exc).replace("\x00", "")[:1000]
        return GrabberIssue(
            code=code,
            message=message,
            stage=stage,
            retryable=retryable,
            item_url=item_url,
            pdf_url=pdf_url,
        )

    @staticmethod
    def _record(
        metadata: ItemMetadata,
        *,
        state: RecordState,
        pdf_url: str | None,
        source_filename: str | None,
        relative_path: str | None,
        expected_md5: str | None,
        bytes_value: int | None = None,
        md5: str | None = None,
        sha256: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        error: BaseException | None = None,
    ) -> ArtifactRecord:
        """Build one complete result record from item metadata."""

        return ArtifactRecord(
            scope=metadata.scope,
            document_id=metadata.document_id,
            source_id=metadata.source_id,
            version=metadata.version,
            item_handle=metadata.item_handle,
            item_url=metadata.item_url,
            title=metadata.title,
            publication_date=metadata.publication_date,
            year=metadata.year,
            doi=metadata.doi,
            rights=metadata.rights,
            pdf_url=pdf_url,
            source_filename=source_filename,
            relative_path=relative_path,
            state=state,
            bytes=bytes_value,
            md5=md5,
            sha256=sha256,
            expected_md5=expected_md5,
            etag=etag,
            last_modified=last_modified,
            error_code=(
                None
                if error is None
                else str(getattr(error, "code", "error"))
            ),
            error_message=(
                None
                if error is None
                else str(error)
            ),
        )

    def grab(self, request: GrabberRequest) -> GrabberResult:
        """Execute one bounded grab request and continue across per-item failures."""

        started_at = self.now()
        records: list[ArtifactRecord] = []
        issues: list[GrabberIssue] = []
        affected = AffectedPeriods()
        seen_item_urls: set[str] = set()
        seen_pdf_urls: set[str] = set()
        processed_items = 0
        blocked = False
        scopes = (
            (Scope.ISSUES, Scope.ARTICLES)
            if request.scope is Scope.ALL
            else (request.scope,)
        )

        self.client.log_policy()
        for scope in scopes:
            if blocked:
                break
            try:
                items = self.collect_scope_items(request, scope)
                for item_handle, item_url, fallback_year in items:
                    if item_url in seen_item_urls:
                        continue
                    if (
                        request.max_items is not None
                        and processed_items >= request.max_items
                    ):
                        break
                    seen_item_urls.add(item_url)
                    processed_items += 1
                    try:
                        metadata = self._metadata(
                            scope=scope,
                            item_handle=item_handle,
                            item_url=item_url,
                            fallback_year=fallback_year,
                        )
                        if metadata.year is not None and not (
                            request.from_year
                            <= metadata.year
                            <= request.to_year
                        ):
                            continue
                        affected.add(
                            metadata.publication_date,
                            metadata.year,
                        )
                        if not metadata.pdfs:
                            records.append(
                                self._record(
                                    metadata,
                                    state=RecordState.NO_PDF,
                                    pdf_url=None,
                                    source_filename=None,
                                    relative_path=None,
                                    expected_md5=None,
                                )
                            )
                            continue
                        for candidate in metadata.pdfs:
                            if candidate.url in seen_pdf_urls:
                                continue
                            seen_pdf_urls.add(candidate.url)
                            relative_path = target_relative_path(
                                metadata,
                                candidate,
                            )
                            if request.dry_run:
                                records.append(
                                    self._record(
                                        metadata,
                                        state=RecordState.PLANNED,
                                        pdf_url=candidate.url,
                                        source_filename=(
                                            candidate.source_name
                                        ),
                                        relative_path=relative_path,
                                        expected_md5=(
                                            candidate.expected_md5
                                        ),
                                    )
                                )
                                continue
                            try:
                                downloaded = download_pdf(
                                    self.client,
                                    candidate,
                                    request.output_root
                                    / Path(relative_path),
                                    allowed_root=request.output_root,
                                    force=request.force,
                                    max_bytes=(
                                        request.max_pdf_bytes
                                        or self.config.max_pdf_bytes
                                    ),
                                )
                                records.append(
                                    self._record(
                                        metadata,
                                        state=downloaded.state,
                                        pdf_url=candidate.url,
                                        source_filename=(
                                            candidate.source_name
                                        ),
                                        relative_path=(
                                            downloaded.relative_path
                                        ),
                                        expected_md5=(
                                            candidate.expected_md5
                                        ),
                                        bytes_value=downloaded.bytes,
                                        md5=downloaded.md5,
                                        sha256=downloaded.sha256,
                                        etag=downloaded.etag,
                                        last_modified=(
                                            downloaded.last_modified
                                        ),
                                    )
                                )
                            except (
                                RobotsDeniedError,
                                RobotsUnavailableError,
                            ) as exc:
                                blocked = True
                                issues.append(
                                    self._issue(
                                        exc,
                                        stage="robots",
                                        item_url=item_url,
                                        pdf_url=candidate.url,
                                    )
                                )
                                records.append(
                                    self._record(
                                        metadata,
                                        state=RecordState.ERROR,
                                        pdf_url=candidate.url,
                                        source_filename=(
                                            candidate.source_name
                                        ),
                                        relative_path=relative_path,
                                        expected_md5=(
                                            candidate.expected_md5
                                        ),
                                        error=exc,
                                    )
                                )
                                break
                            except (
                                GrabberHttpError,
                                PdfDownloadError,
                                OSError,
                            ) as exc:
                                issues.append(
                                    self._issue(
                                        exc,
                                        stage="download",
                                        item_url=item_url,
                                        pdf_url=candidate.url,
                                    )
                                )
                                records.append(
                                    self._record(
                                        metadata,
                                        state=RecordState.ERROR,
                                        pdf_url=candidate.url,
                                        source_filename=(
                                            candidate.source_name
                                        ),
                                        relative_path=relative_path,
                                        expected_md5=(
                                            candidate.expected_md5
                                        ),
                                        error=exc,
                                    )
                                )
                        if blocked:
                            break
                    except (
                        RobotsDeniedError,
                        RobotsUnavailableError,
                    ) as exc:
                        blocked = True
                        issues.append(
                            self._issue(
                                exc,
                                stage="robots",
                                item_url=item_url,
                            )
                        )
                        break
                    except (
                        GrabberHttpError,
                        ValueError,
                        OSError,
                    ) as exc:
                        issues.append(
                            self._issue(
                                exc,
                                stage="item",
                                item_url=item_url,
                            )
                        )
            except (
                RobotsDeniedError,
                RobotsUnavailableError,
            ) as exc:
                blocked = True
                issues.append(self._issue(exc, stage="robots"))
            except (
                GrabberHttpError,
                GrabberServiceError,
                ValueError,
                OSError,
            ) as exc:
                issues.append(self._issue(exc, stage="scope"))

        outcome = result_outcome(
            records,
            issues,
            blocked=blocked,
        )
        return GrabberResult(
            source=self.source_descriptor,
            request=request,
            started_at=started_at,
            finished_at=self.now(),
            outcome=outcome,
            records=tuple(records),
            issues=tuple(issues),
            affected_periods=affected,
        )
