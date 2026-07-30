#!/usr/bin/env python3
"""Compatibility CLI for the modular RKI Epidemiologisches Bulletin grabber."""
from __future__ import annotations

import argparse
from datetime import date
import logging
from pathlib import Path
import sys
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_grabber.api import (  # noqa: E402
    grab,
    write_legacy_outputs,
    write_result,
)
from scripts.rki_grabber.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_source_config,
)
from scripts.rki_grabber.http import (  # noqa: E402
    GrabberHttpError,
    PoliteClient,
)
from scripts.rki_grabber.models import (  # noqa: E402
    GrabberRequest,
    GrabberResult,
    PdfCandidate,
    Scope,
)
from scripts.rki_grabber.parser import (  # noqa: E402
    extract_submission_item_links,
    extract_year_collections,
    find_next_page,
    normalize_handle_url,
    parse_item_metadata,
    parse_listing_bounds,
    safe_component,
    target_relative_path,
    with_offset,
)
from scripts.rki_pipeline.io_utils import stable_json_dumps  # noqa: E402

DEFAULT_USER_AGENT = "RKI-EpidBull-Research-Downloader/2.0"

__all__ = [
    "GrabberRequest",
    "GrabberResult",
    "PdfCandidate",
    "PoliteClient",
    "Scope",
    "extract_submission_item_links",
    "extract_year_collections",
    "find_next_page",
    "grab",
    "main",
    "normalize_handle_url",
    "parse_item_metadata",
    "parse_listing_bounds",
    "safe_component",
    "target_relative_path",
    "with_offset",
]


def build_parser() -> argparse.ArgumentParser:
    """Build the backward-compatible CLI plus the new structured-result switches."""

    parser = argparse.ArgumentParser(
        description=(
            "Lädt PDFs des RKI-Epidemiologischen Bulletins vom offiziellen "
            "Publikationsserver edoc.rki.de oder plant den Abruf seiteneffektfrei."
        )
    )
    parser.add_argument(
        "--scope",
        choices=tuple(scope.value for scope in Scope),
        default=Scope.ISSUES.value,
        help="issues = Gesamtausgaben; articles = Einzelartikel; all = beides",
    )
    parser.add_argument("--from-year", type=int, default=1994)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument("--output", type=Path, default=Path("rki-epidbull"))
    parser.add_argument("--delay", type=float)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--contact", help="Kontaktangabe ausschließlich für den User-Agent")
    parser.add_argument("--user-agent", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Metadaten und geplante PDFs erfassen, aber keine PDFs oder Standard-"
            "Outputdateien schreiben; JSON wird auf stdout ausgegeben"
        ),
    )
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="robots.txt nur nach anderweitig dokumentierter Erlaubnis abschalten",
    )
    parser.add_argument("--max-html-bytes", type=int)
    parser.add_argument("--max-pdf-bytes", type=int)
    parser.add_argument(
        "--result-json",
        "--output-report",
        dest="result_json",
        type=Path,
        help="Expliziter Pfad für das kanonische grabber-result JSON",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version="desinfect RKI grabber 2.0")
    return parser


def _request_from_args(args: argparse.Namespace) -> GrabberRequest:
    """Translate validated argparse values into the stable public request type."""

    return GrabberRequest(
        scope=Scope(args.scope),
        from_year=args.from_year,
        to_year=args.to_year,
        dry_run=args.dry_run,
        max_items=args.max_items,
        force=args.force,
        output_root=args.output,
        result_path=args.result_json,
        contact=args.contact,
        user_agent=args.user_agent,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        respect_robots=False if args.no_robots else None,
        max_html_bytes=args.max_html_bytes,
        max_pdf_bytes=args.max_pdf_bytes,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., GrabberResult] = grab,
) -> int:
    """Execute one CLI request and return the stable 0/2/3/4 exit contract."""

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.no_robots:
        logging.warning(
            "robots.txt-Prüfung ist explizit deaktiviert; die Erlaubnis muss extern "
            "dokumentiert sein."
        )
    try:
        request = _request_from_args(args)
        config = load_source_config(args.config)
        result = runner(request, config=config)
        if request.dry_run:
            if args.result_json is None:
                sys.stdout.write(stable_json_dumps(result.to_dict()))
            else:
                write_result(
                    args.result_json,
                    result,
                    allowed_root=args.result_json.parent,
                )
        else:
            write_legacy_outputs(request.output_root, result)
            default_result = request.output_root / "result.json"
            if args.result_json is not None and args.result_json != default_result:
                write_result(
                    args.result_json,
                    result,
                    allowed_root=args.result_json.parent,
                )
        logging.info(
            "RKI-Grabber abgeschlossen: outcome=%s records=%s issues=%s",
            result.outcome.value,
            len(result.records),
            len(result.issues),
        )
        return result.exit_code
    except (ValueError, OSError, GrabberHttpError) as exc:
        print(f"rki-grabber: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
