"""Contracts for deterministic, rights-bound archive inputs."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.archive import (
    ArchiveBuild,
    ArchiveEntry,
    ArchiveInspection,
    ArchiveLimits,
    ArchiveMaterialization,
    ArchiveSpec,
    _zip_datetime,
    archive_input_fingerprint,
)
from scripts.rki_pipeline.rights import resolve_rights
from scripts.rki_pipeline.storage.base import PreparedObject, RightsStorageAuthorizer


SOURCE_ID = "rki:176904/900000001"
SOURCE_SHA256 = "a" * 64


def _authorizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RightsStorageAuthorizer:
    register = tmp_path / "rights-register.yml"
    register.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "decisions:",
                f"  - source_id: {SOURCE_ID}",
                f"    source_sha256: {SOURCE_SHA256}",
                "    state: approved",
                "    basis: Reviewed RKI reuse terms",
                "    reviewed_by: Legal Reviewer",
                '    reviewed_at: "2026-08-03T08:00:00Z"',
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register)
    return RightsStorageAuthorizer(
        authority=rights.load_rights_authority(),
        policy=rights.load_rights_policy(),
    )


def _prepared_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    visibility: str = "public",
) -> tuple[ArchiveEntry, ArchiveEntry]:
    authorizer = _authorizer(tmp_path, monkeypatch)
    decision = resolve_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authorizer.authority,
        policy=authorizer.policy,
    )
    root = tmp_path / "prepared"
    root.mkdir(exist_ok=True)

    def prepared(path: str, payload: bytes) -> ArchiveEntry:
        source = root / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
        return ArchiveEntry(
            path=path,
            prepared=PreparedObject(
                artifact_id=f"artifact-{path}",
                logical_key=path,
                path=source,
                temp_root=root,
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
                source_id=SOURCE_ID,
                source_sha256=SOURCE_SHA256,
                decision_sha256=decision.decision_sha256,
                visibility=visibility,
                rights_state="approved",
            ),
        )

    return prepared("PDF/first.pdf", b"first payload"), prepared("PDF/second.pdf", b"second payload")


def _spec(
    entries: tuple[ArchiveEntry, ...],
    *,
    archive_id: str = "archive-2026-W01-pdf",
    period: str = "2026-W01",
    kind: str = "week-pdf",
    visibility: str = "public",
    source_date_epoch: int = 0,
) -> ArchiveSpec:
    return ArchiveSpec(
        archive_id=archive_id,
        period=period,
        kind=kind,
        visibility=visibility,
        source_date_epoch=source_date_epoch,
        entries=entries,
    )


def _with_payload(entry: ArchiveEntry, payload: bytes) -> ArchiveEntry:
    path = entry.prepared.temp_root / "changed.pdf"
    path.write_bytes(payload)
    prepared = replace(
        entry.prepared,
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )
    return ArchiveEntry(path=entry.path, prepared=prepared)


def test_deterministic_archive_fingerprint_is_order_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _prepared_entries(tmp_path, monkeypatch)
    left = _spec((first, second), source_date_epoch=1_700_000_001)
    right = _spec((second, first), source_date_epoch=1_700_000_001)
    assert archive_input_fingerprint(left) == archive_input_fingerprint(right)


def test_archive_fingerprint_binds_identity_timestamp_and_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    baseline = archive_input_fingerprint(_spec((entry,), source_date_epoch=1_700_000_001))
    assert archive_input_fingerprint(_spec((entry,), source_date_epoch=1_700_000_003)) != baseline
    assert archive_input_fingerprint(_spec((entry,), period="2026-W02")) != baseline
    assert archive_input_fingerprint(_spec((_with_payload(entry, b"changed payload"),))) != baseline


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (0, (1980, 1, 1, 0, 0, 0)),
        (1_700_000_001, (2023, 11, 14, 22, 13, 20)),
        (9_999_999_999, (2107, 12, 31, 23, 59, 58)),
    ],
)
def test_zip_timestamp_is_clamped_and_even(epoch: int, expected: tuple[int, ...]) -> None:
    assert _zip_datetime(epoch) == expected


def test_archive_spec_accepts_current_rights_prepared_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorizer = _authorizer(tmp_path, monkeypatch)
    first, second = _prepared_entries(tmp_path, monkeypatch)
    authorizer.authorize(first.prepared, operation="archive")
    assert _spec((first, second)).entries == (first, second)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda entry: ArchiveEntry(path=Path("PDF/first.pdf"), prepared=entry.prepared), "path"),
        (lambda entry: ArchiveEntry(path=entry.path, prepared=object()), "prepared"),
        (lambda entry: _spec((entry,), source_date_epoch=True), "source_date_epoch"),
        (lambda entry: _spec([entry]), "entries"),
    ],
)
def test_archive_contract_rejects_wrong_dataclass_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
    match: str,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=match):
        factory(entry)  # type: ignore[operator]


@pytest.mark.parametrize(
    "path",
    (
        "MANIFEST.json",
        "README.md",
        "SHA256SUMS.txt",
        "PDF/nested.zip",
        "PDF/NESTED.ZIP",
        "../escape.pdf",
    ),
)
def test_archive_spec_rejects_reserved_nested_or_unsafe_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        candidate = ArchiveEntry(path=path, prepared=entry.prepared)
        _spec((candidate,))


def test_archive_spec_rejects_duplicate_and_portable_colliding_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _prepared_entries(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Doppelte"):
        _spec((first, ArchiveEntry(first.path, second.prepared)))
    with pytest.raises(ValueError, match="Kollision"):
        _spec((first, ArchiveEntry("pdf/FIRST.pdf", second.prepared)))


def test_archive_spec_rejects_mixed_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public, _ = _prepared_entries(tmp_path, monkeypatch, visibility="public")
    internal, _ = _prepared_entries(tmp_path, monkeypatch, visibility="internal")
    with pytest.raises(ValueError, match="Sichtbarkeit"):
        _spec((public, ArchiveEntry("PDF/internal.pdf", internal.prepared)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_entries", True),
        ("max_member_bytes", 0),
        ("max_total_bytes", -1),
        ("max_archive_bytes", "1"),
        ("max_compression_ratio", 0),
    ],
)
def test_archive_limits_reject_invalid_count_and_size_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        ArchiveLimits(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("archive_id", ("", "../escape", "archive/part", "ARCHIVE"))
def test_archive_spec_rejects_malformed_archive_id(archive_id: str) -> None:
    with pytest.raises(ValueError, match="archive_id"):
        _spec((), archive_id=archive_id)


@pytest.mark.parametrize("kind", ("week-epub", "week_pdf", ""))
def test_archive_spec_rejects_malformed_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        _spec((), kind=kind)


def test_archive_result_contracts_reject_wrong_types(tmp_path: Path) -> None:
    build = ArchiveBuild(
        path=tmp_path / "archive.zip",
        input_fingerprint="a" * 64,
        output_sha256="b" * 64,
        size=1,
        entries=("PDF/first.pdf",),
    )
    assert ArchiveInspection(
        path=tmp_path / "archive.zip",
        input_fingerprint="a" * 64,
        output_sha256="b" * 64,
        size=1,
        entries=("PDF/first.pdf",),
    ).entries == build.entries
    with pytest.raises(ValueError, match="changed"):
        ArchiveMaterialization(
            root=tmp_path,
            zip_path=tmp_path / "archive.zip",
            manifest_path=tmp_path / "archive-manifest.json",
            build=build,
            changed=1,
        )
