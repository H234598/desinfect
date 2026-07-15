#!/usr/bin/env python3
"""Download Robert Koch Institute Epidemiologisches Bulletin PDFs from edoc.rki.de.

The script deliberately uses the official RKI publication server as its index:

* Complete-issue archive/community: https://edoc.rki.de/handle/176904/10
* Individual-article collection:     https://edoc.rki.de/handle/176904/45

It is intentionally single-threaded, rate-limited, resumable, and produces CSV/JSONL
manifests with checksums and provenance URLs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

try:
    import requests
    from bs4 import BeautifulSoup, Tag
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:  # pragma: no cover - friendly runtime error
    raise SystemExit(
        "Fehlende Abhängigkeiten. Bitte ausführen:\n"
        "  python -m pip install requests beautifulsoup4\n"
    ) from exc


BASE_URL = "https://edoc.rki.de"
ISSUES_ROOT_HANDLE = "176904/10"
ARTICLES_HANDLE = "176904/45"
DEFAULT_USER_AGENT = "RKI-EpidBull-Research-Downloader/1.0"

HANDLE_PATH_RE = re.compile(r"^/handle/(?P<handle>176904/[0-9]+(?:\.[0-9]+)?)/?$")
PDF_PATH_RE = re.compile(r"/bitstream/handle/176904/[0-9]+(?:\.[0-9]+)?/.+\.pdf$", re.I)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-[01]\d-[0-3]\d\b")
TOTAL_EN_RE = re.compile(
    r"Now\s+showing\s+items\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d.,]+)", re.I
)
TOTAL_DE_RE = re.compile(
    r"(?:Einträge|Elemente)\s+(\d+)\s*[-–]\s*(\d+)\s+(?:von|aus)\s+([\d.,]+)", re.I
)
MD5_RE = re.compile(r"\bMD5\s*:\s*([0-9a-f]{32})\b", re.I)
DOI_RE = re.compile(r"\b10\.25646/[0-9]+(?:\.[0-9]+)?\b", re.I)


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    source_name: str
    expected_md5: str | None = None


@dataclass
class ManifestRow:
    scope: str
    year: int | None
    title: str
    doi: str | None
    item_handle: str
    item_url: str
    pdf_url: str
    source_filename: str
    local_path: str
    status: str
    bytes: int | None = None
    md5: str | None = None
    sha256: str | None = None
    expected_md5: str | None = None
    error: str | None = None


class PoliteClient:
    """Requests client with retries, delay, and optional robots.txt enforcement."""

    def __init__(
        self,
        *,
        delay: float,
        timeout: float,
        user_agent: str,
        contact: str | None,
        respect_robots: bool,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.user_agent = user_agent + (f" (contact: {contact})" if contact else "")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "de,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            }
        )
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self._last_request_at = 0.0
        self._robots: RobotFileParser | None = None
        self._respect_robots = respect_robots
        if respect_robots:
            self._load_robots()

    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _raw_get(self, url: str, **kwargs: object) -> requests.Response:
        self._sleep_if_needed()
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
        finally:
            self._last_request_at = time.monotonic()
        return response

    def _load_robots(self) -> None:
        robots_url = urljoin(BASE_URL, "/robots.txt")
        try:
            response = self._raw_get(robots_url)
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                self._robots = parser
                logging.info("robots.txt geladen und wird beachtet.")
            elif response.status_code == 404:
                logging.info("Keine robots.txt gefunden; langsames Crawling wird fortgesetzt.")
            else:
                logging.warning(
                    "robots.txt lieferte HTTP %s; Regeln konnten nicht geprüft werden.",
                    response.status_code,
                )
        except requests.RequestException as exc:
            logging.warning("robots.txt konnte nicht geladen werden: %s", exc)

    def allowed(self, url: str) -> bool:
        if not self._respect_robots or self._robots is None:
            return True
        return self._robots.can_fetch(self.user_agent, url)

    def get(self, url: str, **kwargs: object) -> requests.Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt untersagt den Abruf: {url}")
        response = self._raw_get(url, **kwargs)
        response.raise_for_status()
        return response


def soup_from_response(response: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(response.text, "html.parser")


def normalize_handle_url(url: str) -> tuple[str, str] | None:
    absolute = urljoin(BASE_URL, url)
    parsed = urlsplit(absolute)
    if parsed.netloc.lower() != urlsplit(BASE_URL).netloc.lower():
        return None
    match = HANDLE_PATH_RE.match(parsed.path)
    if not match:
        return None
    handle = match.group("handle")
    clean_url = urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return handle, clean_url


def with_offset(url: str, offset: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if offset:
        query["offset"] = str(offset)
    else:
        query.pop("offset", None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def parse_listing_bounds(text: str) -> tuple[int, int, int] | None:
    for regex in (TOTAL_EN_RE, TOTAL_DE_RE):
        match = regex.search(text)
        if match:
            start, end, total_raw = match.groups()
            total = int(re.sub(r"\D", "", total_raw))
            return int(start), int(end), total
    return None


def extract_year_collections(client: PoliteClient) -> dict[int, tuple[str, str]]:
    root_url = f"{BASE_URL}/handle/{ISSUES_ROOT_HANDLE}"
    response = client.get(root_url)
    soup = soup_from_response(response)
    result: dict[int, tuple[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        if not re.fullmatch(r"(?:19|20)\d{2}", text):
            continue
        normalized = normalize_handle_url(anchor["href"])
        if normalized:
            handle, url = normalized
            result[int(text)] = (handle, url)
    if not result:
        raise RuntimeError(
            "Keine Jahrgangs-Sammlungen gefunden. Möglicherweise hat das RKI das HTML geändert."
        )
    return dict(sorted(result.items()))


def extract_submission_item_links(
    soup: BeautifulSoup,
    *,
    excluded_handles: set[str],
) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        normalized = normalize_handle_url(anchor["href"])
        if not normalized:
            continue
        handle, url = normalized
        if handle in excluded_handles:
            continue
        text = anchor.get_text(" ", strip=True)
        # Submission links on RKI edoc contain a publication date/year and title.
        # This avoids breadcrumb/community links without depending on CSS classes.
        if not (ISO_DATE_RE.search(text) or "Epidemiologisches Bulletin" in text or YEAR_RE.search(text)):
            continue
        found[url] = handle
    return [(handle, url) for url, handle in found.items()]


def find_next_page(soup: BeautifulSoup, current_url: str) -> str | None:
    for anchor in soup.find_all("a", href=True):
        rel = " ".join(anchor.get("rel", [])) if isinstance(anchor.get("rel"), list) else str(anchor.get("rel", ""))
        text = anchor.get_text(" ", strip=True).lower()
        classes = " ".join(anchor.get("class", [])) if isinstance(anchor.get("class"), list) else str(anchor.get("class", ""))
        if "next" in rel.lower() or "next" in classes.lower() or text in {"next", "weiter", ">", "›", "»"}:
            return urljoin(current_url, anchor["href"])
    return None


def iter_submission_items(
    client: PoliteClient,
    *,
    collection_handle: str,
    excluded_handles: set[str],
) -> Iterator[tuple[str, str]]:
    """Yield ``(handle, URL)`` from a DSpace recent-submissions listing."""

    base_listing_url = f"{BASE_URL}/handle/{collection_handle}/recent-submissions"
    page_url: str | None = base_listing_url
    seen_pages: set[str] = set()
    seen_items: set[str] = set()
    expected_total: int | None = None

    while page_url:
        if page_url in seen_pages:
            break
        seen_pages.add(page_url)

        logging.debug("Listing: %s", page_url)
        response = client.get(page_url)
        soup = soup_from_response(response)
        page_items = extract_submission_item_links(soup, excluded_handles=excluded_handles)
        new_items = [(h, u) for h, u in page_items if u not in seen_items]

        for handle, url in new_items:
            seen_items.add(url)
            yield handle, url

        listing_text = soup.get_text(" ", strip=True)
        bounds = parse_listing_bounds(listing_text)
        if bounds:
            start, end, expected_total = bounds
            logging.debug(
                "Listing-Grenzen: %s-%s von %s; neu=%s",
                start,
                end,
                expected_total,
                len(new_items),
            )
            if len(seen_items) >= expected_total or end >= expected_total:
                break

        # Prefer the site's own pagination URL. If the theme omits a recognizable
        # next link, fall back to DSpace's long-standing ``offset`` parameter.
        discovered_next = find_next_page(soup, response.url)
        if discovered_next and discovered_next not in seen_pages:
            page_url = discovered_next
            continue

        if bounds:
            start, end, expected_total = bounds
            page_size = max(1, end - start + 1)
            next_offset = end  # listings are 1-based; URL offsets are 0-based
            candidate = with_offset(base_listing_url, next_offset)
            if candidate in seen_pages or (not new_items and len(seen_pages) > 1):
                raise RuntimeError(
                    "Die Pagination lieferte keine neuen Einträge. "
                    "Das RKI könnte den Parameter 'offset' geändert haben."
                )
            page_url = candidate
            continue

        # No count and no next link. A single-page collection is complete.
        page_url = None

    if expected_total is not None and len(seen_items) < expected_total:
        logging.warning(
            "Nur %s von erwarteten %s Item-Links gefunden.", len(seen_items), expected_total
        )


def first_meta(soup: BeautifulSoup, names: Sequence[str]) -> str | None:
    wanted = {name.lower() for name in names}
    for meta in soup.find_all("meta"):
        name = str(meta.get("name", "")).lower()
        prop = str(meta.get("property", "")).lower()
        if name in wanted or prop in wanted:
            content = str(meta.get("content", "")).strip()
            if content:
                return content
    return None


def extract_item_metadata(
    client: PoliteClient,
    *,
    scope: str,
    item_handle: str,
    item_url: str,
    fallback_year: int | None,
) -> tuple[str, int | None, str | None, list[PdfCandidate]]:
    response = client.get(item_url)
    soup = soup_from_response(response)
    page_text = soup.get_text(" ", strip=True)

    title = first_meta(soup, ("dc.title", "dcterms.title", "citation_title", "og:title"))
    if not title:
        heading = soup.find(["h1", "h2"])
        title = heading.get_text(" ", strip=True) if heading else item_handle
    title = re.sub(r"\s+", " ", title).strip()

    date_value = first_meta(
        soup,
        (
            "dc.date",
            "dc.date.issued",
            "dcterms.issued",
            "citation_publication_date",
            "citation_date",
        ),
    )
    year: int | None = None
    for candidate in (date_value or "", page_text[:1500], title):
        match = YEAR_RE.search(candidate)
        if match:
            year = int(match.group(1))
            break
    if year is None:
        year = fallback_year

    doi = first_meta(soup, ("dc.identifier", "citation_doi", "dc.identifier.doi"))
    if doi:
        match = DOI_RE.search(doi)
        doi = match.group(0) if match else None
    if not doi:
        match = DOI_RE.search(page_text)
        doi = match.group(0) if match else None

    pdfs: dict[str, PdfCandidate] = {}
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(response.url, anchor["href"])
        parsed = urlsplit(absolute)
        if parsed.netloc.lower() != urlsplit(BASE_URL).netloc.lower():
            continue
        if not PDF_PATH_RE.search(parsed.path):
            continue
        source_name = anchor.get_text(" ", strip=True) or Path(unquote(parsed.path)).name
        source_name = source_name.split("—", 1)[0].strip()
        if not source_name.lower().endswith(".pdf"):
            source_name = Path(unquote(parsed.path)).name
        expected_md5 = None
        container_text = ""
        parent = anchor.parent
        if isinstance(parent, Tag):
            container_text = parent.get_text(" ", strip=True)
        match = MD5_RE.search(container_text)
        if match:
            expected_md5 = match.group(1).lower()
        # Some versioned DSpace pages expose the same bitstream more than once
        # with different ``sequence`` query values. Deduplicate only when the
        # repository checksum proves that the payload is identical.
        dedupe_key = f"{parsed.path}|{expected_md5 or parsed.query}"
        pdfs[dedupe_key] = PdfCandidate(
            url=absolute,
            source_name=unquote(source_name),
            expected_md5=expected_md5,
        )

    return title, year, doi, list(pdfs.values())


def safe_component(value: str, *, max_length: int = 120) -> str:
    value = unquote(value)
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"[<>:\"/\\|?*]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    if not value:
        value = "datei"
    return value[:max_length].rstrip(" ._")


def target_path_for(
    output_dir: Path,
    *,
    scope: str,
    year: int | None,
    title: str,
    item_handle: str,
    source_filename: str,
) -> Path:
    year_dir = str(year) if year is not None else "unbekanntes-jahr"
    handle_suffix = item_handle.split("/", 1)[1].replace(".", "-")
    title_part = safe_component(title, max_length=100)
    filename_part = safe_component(source_filename, max_length=100)
    filename = f"{title_part}__{handle_suffix}__{filename_part}"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return output_dir / scope / year_dir / filename


def hash_file(path: Path) -> tuple[int, str, str]:
    md5 = hashlib.md5()  # noqa: S324 - used only to verify repository-provided checksum
    sha256 = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
    return total, md5.hexdigest(), sha256.hexdigest()


def validate_pdf_magic(path: Path) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(5)
    if prefix != b"%PDF-":
        raise ValueError(f"Datei ist kein erkennbares PDF (Magic Bytes: {prefix!r})")


def download_pdf(
    client: PoliteClient,
    candidate: PdfCandidate,
    target: Path,
    *,
    force: bool,
) -> tuple[str, int, str, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        validate_pdf_magic(target)
        total, md5, sha256 = hash_file(target)
        if candidate.expected_md5 and md5.lower() != candidate.expected_md5.lower():
            raise ValueError(
                f"Vorhandene Datei hat falsche MD5: {md5} != {candidate.expected_md5}"
            )
        return "vorhanden", total, md5, sha256

    temp = target.with_suffix(target.suffix + ".part")
    if temp.exists():
        temp.unlink()

    response = client.get(candidate.url, stream=True)
    content_type = response.headers.get("Content-Type", "").lower()
    with temp.open("wb") as stream:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                stream.write(chunk)
    try:
        validate_pdf_magic(temp)
        if content_type and "pdf" not in content_type and "octet-stream" not in content_type:
            logging.warning("Unerwarteter Content-Type %s für %s", content_type, candidate.url)
        total, md5, sha256 = hash_file(temp)
        if candidate.expected_md5 and md5.lower() != candidate.expected_md5.lower():
            raise ValueError(f"MD5-Prüfung fehlgeschlagen: {md5} != {candidate.expected_md5}")
        os.replace(temp, target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return "heruntergeladen", total, md5, sha256


def append_jsonl(path: Path, row: ManifestRow) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: Iterable[ManifestRow]) -> None:
    fieldnames = list(ManifestRow.__dataclass_fields__.keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def collect_scope_items(
    client: PoliteClient,
    *,
    scope: str,
    from_year: int,
    to_year: int,
) -> Iterator[tuple[str, str, int | None]]:
    if scope == "issues":
        years = extract_year_collections(client)
        all_collection_handles = {handle for handle, _ in years.values()}
        excluded = {
            ISSUES_ROOT_HANDLE,
            ARTICLES_HANDLE,
            *all_collection_handles,
        }
        selected = [(year, data) for year, data in years.items() if from_year <= year <= to_year]
        if not selected:
            logging.warning(
                "Keine Jahrgangs-Sammlungen im Bereich %s-%s gefunden. Verfügbar: %s-%s",
                from_year,
                to_year,
                min(years),
                max(years),
            )
            return
        for year, (collection_handle, _collection_url) in selected:
            logging.info("Jahrgang %s (Handle %s)", year, collection_handle)
            for item_handle, item_url in iter_submission_items(
                client,
                collection_handle=collection_handle,
                excluded_handles=excluded,
            ):
                yield item_handle, item_url, year
        return

    if scope == "articles":
        excluded = {ISSUES_ROOT_HANDLE, ARTICLES_HANDLE}
        for item_handle, item_url in iter_submission_items(
            client,
            collection_handle=ARTICLES_HANDLE,
            excluded_handles=excluded,
        ):
            yield item_handle, item_url, None
        return

    raise ValueError(f"Unbekannter Scope: {scope}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Lädt PDFs des RKI-Epidemiologischen Bulletins vom offiziellen "
            "Publikationsserver edoc.rki.de herunter."
        )
    )
    parser.add_argument(
        "--scope",
        choices=("issues", "articles", "all"),
        default="issues",
        help=(
            "issues = Gesamtausgaben/Jahrgänge; articles = Einzelartikel; "
            "all = beides (Standard: issues)"
        ),
    )
    parser.add_argument("--from-year", type=int, default=1994, help="Erstes Publikationsjahr")
    parser.add_argument(
        "--to-year", type=int, default=date.today().year, help="Letztes Publikationsjahr"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("rki-epidbull"), help="Zielverzeichnis"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.25,
        help="Mindestpause zwischen HTTP-Abrufen in Sekunden (Standard: 1.25)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP-Timeout in Sekunden")
    parser.add_argument("--contact", help="Kontaktangabe für den User-Agent, z. B. E-Mail")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent")
    parser.add_argument("--dry-run", action="store_true", help="Nur Links/Metadaten erfassen")
    parser.add_argument(
        "--max-items",
        type=int,
        help="Zum Testen höchstens diese Zahl von Item-Seiten verarbeiten",
    )
    parser.add_argument("--force", action="store_true", help="Vorhandene PDFs neu laden")
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help=(
            "robots.txt-Prüfung abschalten. Nur verwenden, wenn du die Erlaubnis anderweitig "
            "geprüft hast; die Drosselung bleibt aktiv."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Ausführlich protokollieren")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.from_year > args.to_year:
        raise SystemExit("--from-year darf nicht größer als --to-year sein.")
    if args.delay < 0:
        raise SystemExit("--delay darf nicht negativ sein.")
    if args.max_items is not None and args.max_items < 1:
        raise SystemExit("--max-items muss mindestens 1 sein.")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir: Path = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "manifest.jsonl"
    csv_path = output_dir / "manifest.csv"
    run_info_path = output_dir / "run-info.json"
    # Each invocation gets a clean manifest; already downloaded PDFs still make the run resumable.
    jsonl_path.write_text("", encoding="utf-8")

    run_info = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": BASE_URL,
        "issues_root": f"{BASE_URL}/handle/{ISSUES_ROOT_HANDLE}",
        "articles_collection": f"{BASE_URL}/handle/{ARTICLES_HANDLE}",
        "scope": args.scope,
        "from_year": args.from_year,
        "to_year": args.to_year,
        "delay_seconds": args.delay,
        "dry_run": args.dry_run,
        "max_items": args.max_items,
        "respect_robots": not args.no_robots,
    }
    run_info_path.write_text(json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8")

    client = PoliteClient(
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
        contact=args.contact,
        respect_robots=not args.no_robots,
    )

    scopes = ("issues", "articles") if args.scope == "all" else (args.scope,)
    rows: list[ManifestRow] = []
    seen_item_urls: set[str] = set()
    seen_pdf_urls: set[str] = set()
    processed_items = 0
    stop_requested = False

    for scope in scopes:
        if stop_requested:
            break
        logging.info("Starte Scope: %s", scope)
        try:
            item_iter = collect_scope_items(
                client,
                scope=scope,
                from_year=args.from_year,
                to_year=args.to_year,
            )
            for item_handle, item_url, fallback_year in item_iter:
                if item_url in seen_item_urls:
                    continue
                if args.max_items is not None and processed_items >= args.max_items:
                    stop_requested = True
                    logging.info("Testgrenze --max-items=%s erreicht.", args.max_items)
                    break
                seen_item_urls.add(item_url)
                processed_items += 1
                try:
                    title, year, doi, pdfs = extract_item_metadata(
                        client,
                        scope=scope,
                        item_handle=item_handle,
                        item_url=item_url,
                        fallback_year=fallback_year,
                    )
                    if year is not None and not (args.from_year <= year <= args.to_year):
                        logging.debug("Außerhalb Jahresfilter: %s (%s)", title, year)
                        continue
                    if not pdfs:
                        row = ManifestRow(
                            scope=scope,
                            year=year,
                            title=title,
                            doi=doi,
                            item_handle=item_handle,
                            item_url=item_url,
                            pdf_url="",
                            source_filename="",
                            local_path="",
                            status="kein-pdf-link",
                        )
                        rows.append(row)
                        append_jsonl(jsonl_path, row)
                        logging.warning("Kein PDF gefunden: %s", item_url)
                        continue

                    for candidate in pdfs:
                        if candidate.url in seen_pdf_urls:
                            continue
                        seen_pdf_urls.add(candidate.url)
                        target = target_path_for(
                            output_dir,
                            scope=scope,
                            year=year,
                            title=title,
                            item_handle=item_handle,
                            source_filename=candidate.source_name,
                        )
                        row = ManifestRow(
                            scope=scope,
                            year=year,
                            title=title,
                            doi=doi,
                            item_handle=item_handle,
                            item_url=item_url,
                            pdf_url=candidate.url,
                            source_filename=candidate.source_name,
                            local_path=str(target.relative_to(output_dir)),
                            status="geplant" if args.dry_run else "offen",
                            expected_md5=candidate.expected_md5,
                        )
                        try:
                            if args.dry_run:
                                logging.info("[DRY] %s -> %s", candidate.url, target)
                            else:
                                status, size, md5, sha256 = download_pdf(
                                    client,
                                    candidate,
                                    target,
                                    force=args.force,
                                )
                                row.status = status
                                row.bytes = size
                                row.md5 = md5
                                row.sha256 = sha256
                                logging.info("%s: %s", status, target)
                        except Exception as exc:  # continue other files; captured in manifest
                            row.status = "fehler"
                            row.error = f"{type(exc).__name__}: {exc}"
                            logging.exception("PDF-Fehler bei %s", candidate.url)
                        rows.append(row)
                        append_jsonl(jsonl_path, row)
                except Exception as exc:  # continue with remaining items
                    row = ManifestRow(
                        scope=scope,
                        year=fallback_year,
                        title=item_handle,
                        doi=None,
                        item_handle=item_handle,
                        item_url=item_url,
                        pdf_url="",
                        source_filename="",
                        local_path="",
                        status="item-fehler",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    rows.append(row)
                    append_jsonl(jsonl_path, row)
                    logging.exception("Item konnte nicht verarbeitet werden: %s", item_url)
        except Exception as exc:
            logging.exception("Scope %s wurde abgebrochen: %s", scope, exc)
            # Keep already downloaded data and still write a final CSV.

    write_csv(csv_path, rows)
    run_info["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    run_info["rows"] = len(rows)
    run_info["status_counts"] = {
        status: sum(1 for row in rows if row.status == status)
        for status in sorted({row.status for row in rows})
    }
    run_info_path.write_text(json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = sum(1 for row in rows if "fehler" in row.status)
    logging.info("Fertig. Manifest: %s", csv_path)
    logging.info("Datensätze: %s; Fehler: %s", len(rows), errors)
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
