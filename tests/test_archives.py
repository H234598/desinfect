"""Contracts for deterministic, rights-bound archive inputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import stat
import struct
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.archive import (
    ArchiveBuild,
    ArchiveEntry,
    ArchiveInspection,
    ArchiveLimits,
    ArchiveMaterialization,
    ArchiveIntegrityError,
    ArchiveSecurityError,
    ArchiveSpec,
    _zip_datetime,
    archive_input_fingerprint,
    build_archive,
    validate_archive,
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


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    mutate_info: object | None = None,
    archive_comment: bytes = b"",
) -> None:
    replacements = replacements or {}
    with ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with ZipFile(destination, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        archive.comment = archive_comment
        for original, payload in members:
            info = ZipInfo(original.filename, original.date_time)
            for field in (
                "compress_type",
                "comment",
                "extra",
                "create_system",
                "external_attr",
                "internal_attr",
            ):
                setattr(info, field, getattr(original, field))
            if mutate_info is not None:
                mutate_info(info)  # type: ignore[operator]
            archive.writestr(info, replacements.get(info.filename, payload))


def _set_encrypted_flag(path: Path) -> None:
    payload = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = 0
        while (position := payload.find(signature, position)) >= 0:
            flags = struct.unpack_from("<H", payload, position + offset)[0]
            struct.pack_into("<H", payload, position + offset, flags | 1)
            position += 4
    path.write_bytes(payload)


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


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
        "manifest.json",
        "README.md",
        "ReadMe.Md",
        "SHA256SUMS.txt",
        "sha256sums.Txt",
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


def test_archive_spec_allows_reserved_metadata_name_below_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    assert _spec((ArchiveEntry("PDF/manifest.json", entry.prepared),)).entries


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


def test_deterministic_builds_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    spec = _spec(entries, source_date_epoch=1_700_000_001)
    authorizer = _authorizer(tmp_path, monkeypatch)

    first = build_archive(spec, tmp_path / "first.zip", authorizer=authorizer)
    second = build_archive(spec, tmp_path / "second.zip", authorizer=authorizer)

    assert first.output_sha256 == second.output_sha256
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    assert first.entries == ("PDF/first.pdf", "PDF/second.pdf")


def test_deterministic_build_has_sorted_members_and_exact_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, first = reversed(_prepared_entries(tmp_path, monkeypatch))
    destination = tmp_path / "archive.zip"
    spec = _spec((second, first), source_date_epoch=1_700_000_001)
    build_archive(spec, destination, authorizer=_authorizer(tmp_path, monkeypatch))

    with ZipFile(destination) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == [
            "MANIFEST.json",
            "PDF/first.pdf",
            "PDF/second.pdf",
            "README.md",
            "SHA256SUMS.txt",
        ]
        for info in infos:
            assert info.date_time == (2023, 11, 14, 22, 13, 20)
            assert info.compress_type == ZIP_STORED
            assert info.create_system == 3
            assert info.external_attr == (stat.S_IFREG | 0o644) << 16
            assert info.extra == b""
            assert info.comment == b""
        manifest_bytes = archive.read("MANIFEST.json")
        manifest = json.loads(manifest_bytes)
        assert manifest_bytes == _canonical_json(manifest)
        assert manifest["entries"] == [
            {
                "bytes": 13,
                "path": "PDF/first.pdf",
                "sha256": hashlib.sha256(b"first payload").hexdigest(),
            },
            {
                "bytes": 14,
                "path": "PDF/second.pdf",
                "sha256": hashlib.sha256(b"second payload").hexdigest(),
            },
        ]
        assert archive.read("SHA256SUMS.txt") == (
            f"{hashlib.sha256(b'first payload').hexdigest()}  PDF/first.pdf\n"
            f"{hashlib.sha256(b'second payload').hexdigest()}  PDF/second.pdf\n"
        ).encode("ascii")


@pytest.mark.parametrize("path", ("../escape.pdf", "/absolute.pdf", "PDF\\bad.pdf", "PDF/nested.ZIP"))
def test_security_validator_rejects_unsafe_or_nested_member_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    spec = _spec((entry,))
    archive_path = tmp_path / "archive.zip"
    build = build_archive(spec, archive_path, authorizer=_authorizer(tmp_path, monkeypatch))

    def rename(info: ZipInfo) -> None:
        if info.filename == entry.path:
            info.filename = path

    _rewrite_archive(archive_path, tmp_path / "unsafe.zip", mutate_info=rename)
    with pytest.raises(ArchiveSecurityError):
        validate_archive(
            tmp_path / "unsafe.zip",
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256((tmp_path / "unsafe.zip").read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("collision", ("pdf/FIRST.pdf", "PDF/fi\N{COMBINING ACUTE ACCENT}rst.pdf"))
def test_security_validator_rejects_case_or_nfc_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec(entries), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    with ZipFile(archive_path, "a", compression=ZIP_STORED) as archive:
        source = archive.getinfo("PDF/first.pdf")
        info = ZipInfo(collision, source.date_time)
        info.create_system = 3
        info.compress_type = ZIP_STORED
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, b"collision")

    with pytest.raises(ArchiveSecurityError, match="Kollision|kanonisch"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )


def test_security_builder_rejects_symlink_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    original = entry.prepared.path
    moved = original.with_suffix(".real")
    original.rename(moved)
    original.symlink_to(moved)

    with pytest.raises(ArchiveSecurityError, match="regulär|Symlink"):
        build_archive(
            _spec((entry,)),
            tmp_path / "archive.zip",
            authorizer=_authorizer(tmp_path, monkeypatch),
        )


def test_security_builder_rechecks_stale_rights_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    stale = ArchiveEntry(entry.path, replace(entry.prepared, decision_sha256="b" * 64))

    with pytest.raises(ArchiveSecurityError, match="Rechteentscheidung"):
        build_archive(
            _spec((stale,)),
            tmp_path / "archive.zip",
            authorizer=_authorizer(tmp_path, monkeypatch),
        )


def test_security_validator_rejects_mixed_manifest_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    with ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
    manifest["visibility"] = "internal"
    tampered = tmp_path / "mixed.zip"
    _rewrite_archive(archive_path, tampered, replacements={"MANIFEST.json": _canonical_json(manifest)})

    with pytest.raises(ArchiveIntegrityError, match="Fingerprint|Sichtbarkeit"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_encrypted_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    _set_encrypted_flag(archive_path)

    with pytest.raises(ArchiveSecurityError, match="verschlüsselt"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("external_attr", (stat.S_IFLNK | 0o777) << 16, "Modus"),
        ("date_time", (2024, 1, 1, 0, 0, 0), "Zeit"),
        ("extra", b"\x01\x00\x00\x00", "Extra"),
        ("comment", b"comment", "Kommentar"),
    ),
)
def test_security_validator_rejects_unexpected_member_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    match: str,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))

    def mutate(info: ZipInfo) -> None:
        if info.filename == entry.path:
            setattr(info, field, value)

    tampered = tmp_path / "metadata.zip"
    _rewrite_archive(archive_path, tampered, mutate_info=mutate)
    with pytest.raises(ArchiveSecurityError, match=match):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_archive_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "commented.zip"
    _rewrite_archive(source, tampered, archive_comment=b"comment")
    with pytest.raises(ArchiveSecurityError, match="Kommentar"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_duplicate_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    with pytest.warns(UserWarning, match="Duplicate name"):
        with ZipFile(archive_path, "a") as archive:
            archive.writestr(archive.getinfo(entry.path), b"duplicate")

    with pytest.raises(ArchiveSecurityError, match="Doppelt"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        )


def test_security_builder_and_validator_enforce_size_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    spec = _spec(entries)
    authorizer = _authorizer(tmp_path, monkeypatch)
    with pytest.raises(ArchiveSecurityError, match="Anzahl"):
        build_archive(spec, tmp_path / "count.zip", authorizer=authorizer, limits=ArchiveLimits(max_entries=1))
    with pytest.raises(ArchiveSecurityError, match="Mitglied"):
        build_archive(spec, tmp_path / "member.zip", authorizer=authorizer, limits=ArchiveLimits(max_member_bytes=10))
    with pytest.raises(ArchiveSecurityError, match="Gesamt"):
        build_archive(spec, tmp_path / "total.zip", authorizer=authorizer, limits=ArchiveLimits(max_total_bytes=20))

    archive_path = tmp_path / "archive.zip"
    build = build_archive(spec, archive_path, authorizer=authorizer)
    with pytest.raises(ArchiveSecurityError, match="Archivgröße"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=build.output_sha256,
            limits=ArchiveLimits(max_archive_bytes=archive_path.stat().st_size - 1),
        )


def test_security_validator_rejects_excessive_compression_ratio(tmp_path: Path) -> None:
    archive_path = tmp_path / "ratio.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("PDF/bomb.pdf", b"0" * 10_000)
    with pytest.raises(ArchiveSecurityError, match="Kompressionsverhältnis"):
        validate_archive(
            archive_path,
            expected_fingerprint="a" * 64,
            expected_output_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            limits=ArchiveLimits(max_compression_ratio=2),
        )


def test_security_validator_rejects_bad_crc_without_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    payload = archive_path.read_bytes().replace(b"first payload", b"First payload", 1)
    assert payload != archive_path.read_bytes()
    archive_path.write_bytes(payload)
    with pytest.raises(ArchiveIntegrityError, match="CRC|beschädigt"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_security_validator_rejects_payload_content_sha_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    with ZipFile(source) as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
    manifest["entries"][0]["sha256"] = "b" * 64
    tampered = tmp_path / "sha.zip"
    _rewrite_archive(source, tampered, replacements={"MANIFEST.json": _canonical_json(manifest)})
    with pytest.raises(ArchiveIntegrityError, match="SHA-256|Fingerprint"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    "manifest",
    (
        b'{"format_version":"1","format_version":"1"}\n',
        b'{"value":NaN}\n',
        b'{"value": 1}\n',
        b"not json\n",
    ),
)
def test_security_validator_rejects_malformed_or_noncanonical_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: bytes,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "manifest.zip"
    _rewrite_archive(source, tampered, replacements={"MANIFEST.json": manifest})
    with pytest.raises(ArchiveIntegrityError, match="Manifest|JSON"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("mutation", ("wrong-kind-type", "unsafe-entry-path"))
def test_security_validator_wraps_semantically_invalid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    with ZipFile(source) as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
    if mutation == "wrong-kind-type":
        manifest["kind"] = []
    else:
        manifest["entries"][0]["path"] = "../escape.pdf"
    tampered = tmp_path / "semantic.zip"
    _rewrite_archive(source, tampered, replacements={"MANIFEST.json": _canonical_json(manifest)})

    with pytest.raises(ArchiveIntegrityError, match="Manifest"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    "checksums",
    (
        b"broken\n",
        f"{'a' * 64} *PDF/first.pdf\n".encode("ascii"),
        f"{'a' * 64}  ../escape.pdf\n".encode("ascii"),
        b"",
    ),
)
def test_security_validator_rejects_malformed_checksums(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checksums: bytes,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "checksums.zip"
    _rewrite_archive(source, tampered, replacements={"SHA256SUMS.txt": checksums})
    with pytest.raises(ArchiveIntegrityError, match="Checksum"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )
