from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.rki_pipeline.reconciliation import (
    FindingCode,
    ReconciliationCounts,
    ReconciliationFinding,
    SubjectKind,
    build_reconciliation_result,
    source_subject_id,
)
from scripts.rki_pipeline.schema_registry import validate_document


def finding(code: FindingCode, subject_id: str) -> ReconciliationFinding:
    return ReconciliationFinding(
        code=code,
        subject_kind=SubjectKind.SOURCE,
        subject_id=subject_id,
        relative_path=None,
        message=code.value,
    )


def test_result_sorts_findings_and_maps_new_to_missing_local() -> None:
    result = build_reconciliation_result(
        as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
        from_year=1996,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=(
            finding(FindingCode.OK, "source-b"),
            finding(FindingCode.NEW, "source-c"),
            finding(FindingCode.CHANGED, "source-a"),
        ),
    )
    assert [item.code for item in result.findings] == [
        FindingCode.CHANGED,
        FindingCode.NEW,
        FindingCode.OK,
    ]
    assert result.counts == ReconciliationCounts(
        ok=1,
        changed=1,
        missing_remote=0,
        missing_local=1,
        orphan=0,
        rights_changed=0,
        unresolved=2,
    )
    assert result.conclusion == "blocked"
    assert result.successful_at is None
    validate_document("reconciliation-report", result.report)


def test_result_success_records_as_of_as_successful_at() -> None:
    as_of = datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)

    result = build_reconciliation_result(
        as_of=as_of,
        from_year=1996,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=(finding(FindingCode.OK, "source-a"),),
    )

    assert result.conclusion == "success"
    assert result.successful_at == as_of
    assert result.report == {
        "schema_version": "1.0.0",
        "scope": {"from_year": 1996, "to_year": 2026},
        "as_of": "2026-08-04T04:00:00Z",
        "counts": {
            "ok": 1,
            "changed": 0,
            "missing_remote": 0,
            "missing_local": 0,
            "orphan": 0,
            "rights_changed": 0,
            "unresolved": 0,
        },
        "conclusion": "success",
        "source_manifest_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    ("as_of", "from_year", "to_year", "source_manifest_sha256"),
    [
        (datetime(2026, 8, 4, 4, 0), 1996, 2026, "a" * 64),
        (
            datetime(2026, 8, 4, 4, 0, tzinfo=timezone(timedelta(hours=2))),
            1996,
            2026,
            "a" * 64,
        ),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 2027, 2026, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1989, 2026, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1996, 10_000, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1996, 2026, "A" * 64),
    ],
)
def test_result_rejects_noncanonical_metadata(
    as_of: datetime,
    from_year: int,
    to_year: int,
    source_manifest_sha256: str,
) -> None:
    with pytest.raises(ValueError):
        build_reconciliation_result(
            as_of=as_of,
            from_year=from_year,
            to_year=to_year,
            source_manifest_sha256=source_manifest_sha256,
            findings=(finding(FindingCode.OK, "source-a"),),
        )

def test_result_rejects_duplicate_finding_keys() -> None:
    item = finding(FindingCode.OK, "source-a")

    with pytest.raises(ValueError, match="doppelt"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
            findings=(item, item),
        )


def test_result_rejects_ok_mixed_with_unresolved_finding_for_source() -> None:
    with pytest.raises(ValueError, match="ok"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
            findings=(
                finding(FindingCode.OK, source_subject_id("source-a", "bitstream-a")),
                finding(FindingCode.CHANGED, source_subject_id("source-a", "bitstream-b")),
            ),
        )


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("/absolute/path.pdf", "valid"),
        ("relative/path.pdf", "contains\x00control"),
        ("relative/path.pdf", "x" * 501),
    ],
)
def test_finding_rejects_unsafe_path_and_message(
    relative_path: str,
    message: str,
) -> None:
    with pytest.raises(ValueError):
        ReconciliationFinding(
            code=FindingCode.CHANGED,
            subject_kind=SubjectKind.STORAGE,
            subject_id="artifact-a",
            relative_path=relative_path,
            message=message,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "artifacts/../report.pdf",
        "./report.pdf",
        "artifacts//report.pdf",
        "artifacts\\report.pdf",
        "C:report.pdf",
        "cafe\u0301.pdf",
    ],
)
def test_finding_rejects_noncanonical_relative_path_aliases(relative_path: str) -> None:
    with pytest.raises(ValueError):
        ReconciliationFinding(
            code=FindingCode.CHANGED,
            subject_kind=SubjectKind.STORAGE,
            subject_id="artifact-a",
            relative_path=relative_path,
            message="changed",
        )


def test_counts_reject_boolean_values() -> None:
    with pytest.raises(ValueError, match="Ganzzahl"):
        ReconciliationCounts(
            ok=True,
            changed=0,
            missing_remote=0,
            missing_local=0,
            orphan=0,
            rights_changed=0,
            unresolved=0,
        )
