"""End-to-end offline API, schema, and compatibility CLI tests for P03."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable

from jsonschema import Draft202012Validator, FormatChecker

from scripts.rki_grabber.api import grab
from scripts.rki_grabber.models import GrabberRequest, Outcome, RecordState, Scope, SourceConfig
from scripts.rki_grabber.rki_epidbull_grabber import main
from tests.fakes import FakeResponse, FakeTransport

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
BASE = "https://edoc.rki.de"
ROBOTS = f"{BASE}/robots.txt"
ISSUES_ROOT = f"{BASE}/handle/176904/10"
LISTING = f"{BASE}/handle/176904/1996/recent-submissions"
ITEM = f"{BASE}/handle/176904/12345.2"
PDF_URL = f"{BASE}/bitstream/handle/176904/12345.2/minimal.pdf?sequence=2"


def html(name: str) -> bytes:
    """Read one HTML fixture as bytes."""

    return (FIXTURES / "rki-html" / name).read_bytes()


def responses(*, include_pdf: bool) -> dict[str, FakeResponse]:
    """Build the complete finite fake source graph."""

    result = {
        ROBOTS: FakeResponse(404, ROBOTS),
        ISSUES_ROOT: FakeResponse(
            200,
            ISSUES_ROOT,
            html("p03-root.html"),
            {"content-type": "text/html"},
        ),
        LISTING: FakeResponse(
            200,
            LISTING,
            html("p03-listing.html"),
            {"content-type": "text/html"},
        ),
        ITEM: FakeResponse(
            200,
            ITEM,
            html("p03-item.html"),
            {
                "content-type": "text/html",
                "etag": '"item-v2"',
                "last-modified": "Fri, 22 Mar 1996 12:00:00 GMT",
            },
        ),
    }
    if include_pdf:
        pdf = (FIXTURES / "pdf" / "minimal.pdf").read_bytes()
        result[PDF_URL] = FakeResponse(
            200,
            PDF_URL,
            pdf,
            {"content-type": "application/pdf", "etag": '"pdf-v2"'},
        )
    return result


def clock() -> Callable[[], str]:
    """Return a deterministic two-value timestamp callable."""

    values = iter(("2026-07-28T12:00:00Z", "2026-07-28T12:00:01Z"))
    return lambda: next(values)


def validate_result(payload: dict) -> None:
    """Validate against the checked-in P03 result schema."""

    schema = json.loads((ROOT / "schemas" / "grabber-result.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_dry_run_is_importable_structured_and_does_not_create_output(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist"
    result = grab(
        GrabberRequest(
            scope=Scope.ISSUES,
            from_year=1996,
            to_year=1996,
            dry_run=True,
            max_items=1,
            output_root=output,
            delay_seconds=0,
        ),
        config=SourceConfig(delay_seconds=0, timeout_seconds=1),
        transport=FakeTransport(responses(include_pdf=False)),
        now=clock(),
    )
    assert result.outcome is Outcome.SUCCESS
    assert result.exit_code == 0
    assert len(result.records) == 1
    assert result.records[0].state is RecordState.PLANNED
    assert not output.exists()
    payload = result.to_dict()
    validate_result(payload)
    assert payload["affected_periods"] == {
        "weeks": ["1996-W12"],
        "months": ["1996-03"],
        "years": [1996],
    }
    assert "contact" not in payload["request"]


def test_materializing_api_downloads_to_relative_path_and_validates_schema(tmp_path: Path) -> None:
    result = grab(
        GrabberRequest(
            scope=Scope.ISSUES,
            from_year=1996,
            to_year=1996,
            dry_run=False,
            max_items=1,
            output_root=tmp_path,
            delay_seconds=0,
        ),
        config=SourceConfig(delay_seconds=0, timeout_seconds=1),
        transport=FakeTransport(responses(include_pdf=True)),
        now=clock(),
    )
    assert result.outcome is Outcome.SUCCESS
    record = result.records[0]
    assert record.state is RecordState.DOWNLOADED
    assert record.relative_path is not None
    assert not Path(record.relative_path).is_absolute()
    assert (tmp_path / record.relative_path).is_file()
    validate_result(result.to_dict())


def test_cli_and_api_share_result_contract_without_default_dry_run_writes(
    tmp_path: Path,
    capsys,
) -> None:
    expected = grab(
        GrabberRequest(
            scope=Scope.ISSUES,
            from_year=1996,
            to_year=1996,
            dry_run=True,
            max_items=1,
            output_root=tmp_path / "unused",
            delay_seconds=0,
        ),
        config=SourceConfig(delay_seconds=0, timeout_seconds=1),
        transport=FakeTransport(responses(include_pdf=False)),
        now=clock(),
    )

    def runner(request: GrabberRequest, **_kwargs):
        assert request.dry_run
        return expected

    output = tmp_path / "cli-output"
    exit_code = main(
        [
            "--scope",
            "issues",
            "--from-year",
            "1996",
            "--to-year",
            "1996",
            "--max-items",
            "1",
            "--dry-run",
            "--output",
            str(output),
            "--config",
            str(ROOT / "config" / "rki-source.toml"),
        ],
        runner=runner,
    )
    assert exit_code == expected.exit_code
    assert not output.exists()
    stdout = json.loads(capsys.readouterr().out)
    assert stdout == expected.to_dict()


def test_cli_configuration_error_uses_exit_code_four(capsys) -> None:
    exit_code = main(["--from-year", "2027", "--to-year", "2026"])
    assert exit_code == 4
    assert "from_year" in capsys.readouterr().err
