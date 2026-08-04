"""Deterministic reconciliation findings and report contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Iterable
import unicodedata

from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.schema_registry import validate_document


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FindingCode(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    MISSING_REMOTE = "missing_remote"
    MISSING_LOCAL = "missing_local"
    ORPHAN = "orphan"
    RIGHTS_CHANGED = "rights_changed"
    OK = "ok"


class SubjectKind(StrEnum):
    SOURCE = "source"
    STORAGE = "storage"
    PERIOD = "period"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: FindingCode
    subject_kind: SubjectKind
    subject_id: str
    relative_path: str | None
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not FindingCode or type(self.subject_kind) is not SubjectKind:
            raise ValueError("Finding-Code und Subject-Kind müssen kanonisch sein")
        if type(self.subject_id) is not str or _has_control_character(self.subject_id):
            raise ValueError("subject_id enthält Steuerzeichen oder ist ungültig")
        if self.relative_path is not None:
            if (
                type(self.relative_path) is not str
                or _has_control_character(self.relative_path)
                or PurePosixPath(self.relative_path).is_absolute()
                or PureWindowsPath(self.relative_path).is_absolute()
            ):
                raise ValueError("relative_path ist nicht relativ oder enthält Steuerzeichen")
        if (
            type(self.message) is not str
            or len(self.message) > 500
            or _has_control_character(self.message)
        ):
            raise ValueError("message ist ungültig")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.code.value,
            self.subject_kind.value,
            self.subject_id,
            self.relative_path or "",
        )


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    ok: int
    changed: int
    missing_remote: int
    missing_local: int
    orphan: int
    rights_changed: int
    unresolved: int

    def __post_init__(self) -> None:
        for value in (
            self.ok,
            self.changed,
            self.missing_remote,
            self.missing_local,
            self.orphan,
            self.rights_changed,
            self.unresolved,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("Counts müssen nichtnegative Ganzzahlen sein")

    def to_dict(self) -> dict[str, int]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "missing_remote": self.missing_remote,
            "missing_local": self.missing_local,
            "orphan": self.orphan,
            "rights_changed": self.rights_changed,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    findings: tuple[ReconciliationFinding, ...]
    counts: ReconciliationCounts
    conclusion: str
    source_manifest_sha256: str
    report: dict[str, object]
    successful_at: datetime | None


def source_subject_id(source_id: str, bitstream_id: str) -> str:
    return f"{source_id}#{bitstream_id}"


def build_reconciliation_result(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    source_manifest_sha256: str,
    findings: Iterable[ReconciliationFinding],
) -> ReconciliationResult:
    if (
        type(as_of) is not datetime
        or as_of.tzinfo is None
        or as_of.utcoffset() != timedelta(0)
    ):
        raise ValueError("as_of muss UTC-aware sein")
    if (
        type(from_year) is not int
        or type(to_year) is not int
        or not 1990 <= from_year <= to_year <= 9999
    ):
        raise ValueError("Jahresbereich ist ungültig")
    if type(source_manifest_sha256) is not str or _SHA256.fullmatch(source_manifest_sha256) is None:
        raise ValueError("source_manifest_sha256 muss ein kleingeschriebener SHA-256 sein")

    ordered: list[ReconciliationFinding] = []
    keys: set[tuple[str, str, str, str]] = set()
    source_states: dict[str, set[FindingCode]] = {}
    counts = {
        "ok": 0,
        "changed": 0,
        "missing_remote": 0,
        "missing_local": 0,
        "orphan": 0,
        "rights_changed": 0,
        "unresolved": 0,
    }
    for item in findings:
        if type(item) is not ReconciliationFinding:
            raise ValueError("findings müssen ReconciliationFinding sein")
        if item.key in keys:
            raise ValueError("Finding-Key ist doppelt")
        keys.add(item.key)
        ordered.append(item)
        if item.subject_kind is SubjectKind.SOURCE:
            source_states.setdefault(item.subject_id.partition("#")[0], set()).add(item.code)
        if item.code is FindingCode.NEW:
            counts["missing_local"] += 1
        else:
            counts[item.code.value] += 1
        if item.code is not FindingCode.OK:
            counts["unresolved"] += 1

    if any(FindingCode.OK in codes and len(codes) > 1 for codes in source_states.values()):
        raise ValueError("ok darf nicht mit offenem Finding derselben Quelle gemischt werden")

    result_counts = ReconciliationCounts(**counts)
    conclusion = "success" if result_counts.unresolved == 0 else "blocked"
    report: dict[str, object] = json.loads(
        stable_json_dumps(
            {
                "schema_version": "1.0.0",
                "scope": {"from_year": from_year, "to_year": to_year},
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "counts": result_counts.to_dict(),
                "conclusion": conclusion,
                "source_manifest_sha256": source_manifest_sha256,
            }
        )
    )
    validate_document("reconciliation-report", report)
    return ReconciliationResult(
        findings=tuple(sorted(ordered, key=lambda item: item.key)),
        counts=result_counts,
        conclusion=conclusion,
        source_manifest_sha256=source_manifest_sha256,
        report=report,
        successful_at=as_of if conclusion == "success" else None,
    )


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)
