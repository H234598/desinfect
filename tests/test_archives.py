"""Contracts for deterministic, rights-bound archive inputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import stat
import struct
import tempfile
import tracemalloc
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from scripts.rki_pipeline import archive as archive_module
from scripts.rki_pipeline import cli as pipeline_cli
from scripts.rki_pipeline import rights
from scripts.rki_pipeline import staging as staging_module
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
    materialize_archive,
    validate_archive,
)
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.rights import resolve_rights
from scripts.rki_pipeline.run_modes import (
    capture_repository_snapshot,
    EffectKind,
    EffectLedger,
    RunMode,
)
from scripts.rki_pipeline.storage.base import PreparedObject, RightsStorageAuthorizer
from scripts.rki_pipeline.staging import StagingError


SOURCE_ID = "rki:176904/900000001"
SOURCE_SHA256 = "a" * 64
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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
    archive_id: str = "archive-2026-w01-pdf",
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


def test_build_archive_pilot_cli_is_offline_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    temp_parent = tmp_path / "system-temp"
    temp_parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_parent))
    before = capture_repository_snapshot(
        REPOSITORY_ROOT,
        protected_paths=("status.json",),
        temp_root=None,
    )
    command = ["build-archive", "--fixture", "pilot", "--mode", "materialize"]

    assert pipeline_cli.main(command) == 0
    first_output = capsys.readouterr().out
    assert pipeline_cli.main(command) == 0
    second_output = capsys.readouterr().out

    payload = json.loads(first_output)
    assert set(payload) == {
        "bytes",
        "changed",
        "input_fingerprint",
        "output_sha256",
    }
    assert payload["changed"] is True
    assert type(payload["bytes"]) is int and payload["bytes"] > 0
    assert len(payload["input_fingerprint"]) == 64
    assert len(payload["output_sha256"]) == 64
    assert first_output == second_output
    assert first_output == (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    )
    assert list(temp_parent.iterdir()) == []
    assert capture_repository_snapshot(
        REPOSITORY_ROOT,
        protected_paths=("status.json",),
        temp_root=None,
    ) == before


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--fixture", "unknown", "--mode", "materialize"), "fixture muss pilot sein"),
        (("--fixture", "pilot", "--mode", "plan"), "mode muss materialize sein"),
    ),
)
def test_build_archive_cli_rejects_unsupported_values(
    arguments: tuple[str, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert pipeline_cli.main(["build-archive", *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"build-archive: {message}\n"


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


def _rewrite_archive_with_zip64_member(source: Path, destination: Path, member: str) -> None:
    with ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with ZipFile(destination, "w", compression=ZIP_STORED, allowZip64=True) as archive:
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
            with archive.open(info, "w", force_zip64=info.filename == member) as handle:
                handle.write(payload)


def _add_zip64_end_records(source: Path, destination: Path) -> None:
    payload = source.read_bytes()
    eocd_offset = payload.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    (
        _signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2IH", payload, eocd_offset)
    assert (disk_number, central_disk, comment_size) == (0, 0, 0)
    zip64_eocd = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, eocd_offset, 1)
    destination.write_bytes(payload[:eocd_offset] + zip64_eocd + locator + payload[eocd_offset:])


def _insert_gap_before_central_directory(source: Path, destination: Path) -> None:
    payload = bytearray(source.read_bytes())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    central_offset = struct.unpack_from("<I", payload, eocd_offset + 16)[0]
    gap = b"unreferenced interstitial bytes"
    shifted = payload[:central_offset] + gap + payload[central_offset:]
    shifted_eocd_offset = eocd_offset + len(gap)
    struct.pack_into("<I", shifted, shifted_eocd_offset + 16, central_offset + len(gap))
    destination.write_bytes(shifted)


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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("archive_id", "archive-A"),
        ("archive_id", "ab"),
        ("archive_id", "a" * 202),
        ("period", "W01"),
        ("period", "p" * 41),
    ),
)
def test_archive_spec_rejects_sidecar_incompatible_identity_fields(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError, match=field):
        _spec((), **{field: value})


def test_archive_entry_rejects_sidecar_incompatible_path_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="path"):
        ArchiveEntry(path="P/" + "a" * 499, prepared=entry.prepared)


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


def test_builder_streams_multiple_payloads_with_bounded_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = _prepared_entries(tmp_path, monkeypatch)
    payload_size = 2 * 1024 * 1024
    payload = b"x" * payload_size
    entries: list[ArchiveEntry] = []
    for index in range(4):
        source = base.prepared.temp_root / f"stream-{index}.pdf"
        source.write_bytes(payload)
        entries.append(
            ArchiveEntry(
                path=f"PDF/stream-{index}.pdf",
                prepared=replace(
                    base.prepared,
                    artifact_id=f"artifact-stream-{index}",
                    logical_key=f"PDF/stream-{index}.pdf",
                    path=source,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size=payload_size,
                ),
            )
        )
    del payload

    tracemalloc.start()
    try:
        build_archive(
            _spec(tuple(entries)),
            tmp_path / "streamed.zip",
            authorizer=_authorizer(tmp_path, monkeypatch),
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 5 * 1024 * 1024


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


def test_deterministic_build_supports_nfc_unicode_payload_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    unicode_entry = ArchiveEntry("PDF/überblick.pdf", entry.prepared)
    spec = _spec((unicode_entry,))
    authorizer = _authorizer(tmp_path, monkeypatch)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_build = build_archive(spec, first, authorizer=authorizer)
    second_build = build_archive(spec, second, authorizer=authorizer)

    assert first_build.output_sha256 == second_build.output_sha256
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert archive.read("SHA256SUMS.txt") == (
            f"{entry.prepared.sha256}  PDF/überblick.pdf\n".encode("utf-8")
        )


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


def test_security_builder_rejects_symlinked_payload_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    prepared_parent = entry.prepared.path.parent
    outside_parent = tmp_path / "outside"
    prepared_parent.rename(outside_parent)
    prepared_parent.symlink_to(outside_parent, target_is_directory=True)

    with pytest.raises(ArchiveSecurityError, match="Symlink|Pfadkomponente"):
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


def test_security_validator_rejects_path_swap_after_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(archive_path.read_bytes())
    original_zip_file = archive_module.ZipFile
    swapped = False

    class SwappingZipFile(original_zip_file):
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            nonlocal swapped
            if not swapped:
                replacement.replace(archive_path)
                swapped = True
            super().__init__(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_module, "ZipFile", SwappingZipFile)
    with pytest.raises(ArchiveSecurityError, match="Identität|ausgetauscht"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=build.output_sha256,
        )


def test_security_validator_rejects_growth_after_hash_over_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    archive_path = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), archive_path, authorizer=_authorizer(tmp_path, monkeypatch))
    original_size = archive_path.stat().st_size
    original_zip_file = archive_module.ZipFile
    grown = False

    class GrowingZipFile(original_zip_file):
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            nonlocal grown
            if not grown:
                with archive_path.open("ab") as handle:
                    handle.write(b"x")
                grown = True
            super().__init__(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_module, "ZipFile", GrowingZipFile)
    with pytest.raises(ArchiveSecurityError, match="Archivgröße"):
        validate_archive(
            archive_path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=build.output_sha256,
            limits=ArchiveLimits(max_archive_bytes=original_size),
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


@pytest.mark.parametrize("member", tuple(sorted(archive_module.RESERVED_MEMBERS)))
def test_security_validator_rejects_oversized_internal_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / f"oversized-{member}.zip"
    _rewrite_archive(source, tampered, replacements={member: b"x" * (16 * 1024)})

    with pytest.raises(ArchiveSecurityError, match="Metadaten|metadata|Limit"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
            limits=ArchiveLimits(max_entries=1),
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


def test_security_validator_rejects_zip64_version_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "zip64.zip"
    _rewrite_archive_with_zip64_member(source, tampered, entry.path)
    with ZipFile(tampered) as archive:
        assert archive.getinfo(entry.path).extract_version == 45

    with pytest.raises(ArchiveSecurityError, match="Version|ZIP64"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_zip64_end_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "zip64-end.zip"
    _add_zip64_end_records(source, tampered)
    with ZipFile(tampered) as archive:
        assert archive.read(entry.path) == b"first payload"

    with pytest.raises(ArchiveSecurityError, match="ZIP64"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_prefixed_polyglot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "polyglot.zip"
    tampered.write_bytes(b"MZ" + source.read_bytes())
    with ZipFile(tampered) as archive:
        assert archive.read(entry.path) == b"first payload"

    with pytest.raises(ArchiveSecurityError, match="Präfix|Container"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
        )


def test_security_validator_rejects_gap_before_central_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _prepared_entries(tmp_path, monkeypatch)
    source = tmp_path / "archive.zip"
    build = build_archive(_spec((entry,)), source, authorizer=_authorizer(tmp_path, monkeypatch))
    tampered = tmp_path / "interstitial.zip"
    _insert_gap_before_central_directory(source, tampered)
    with ZipFile(tampered) as archive:
        assert archive.read(entry.path) == b"first payload"

    with pytest.raises(ArchiveSecurityError, match="Kette|Lücke|lückenlos|Central"):
        validate_archive(
            tampered,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=hashlib.sha256(tampered.read_bytes()).hexdigest(),
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


def test_materialization_writes_canonical_schema_sidecar_and_two_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    result = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(tmp_path, monkeypatch),
    )

    expected = {
        "schema_version": "1.0.0",
        "archive_id": spec.archive_id,
        "period": spec.period,
        "kind": spec.kind,
        "entries": ["PDF/first.pdf", "PDF/second.pdf"],
        "input_fingerprint": result.build.input_fingerprint,
        "output_sha256": result.build.output_sha256,
        "storage_reference": None,
    }
    assert result.changed is True
    assert result.zip_path.name == "archive.zip"
    assert result.manifest_path.name == "archive-manifest.json"
    assert result.manifest_path.read_text(encoding="utf-8") == stable_json_dumps(expected)
    assert [event.kind for event in ledger.events] == [EffectKind.TEMP_FILE] * 2
    assert [event.target for event in ledger.events] == [
        result.zip_path.as_posix(),
        result.manifest_path.as_posix(),
    ]


def test_sidecar_limit_scales_beyond_legacy_cap_for_schema_maximum() -> None:
    limits = ArchiveLimits(max_entries=10_000)

    expected = 4 * 1024 + limits.max_entries * (500 * 6 + 16)
    assert archive_module._sidecar_byte_limit(limits) == expected
    assert expected > 8 * 1024 * 1024


def test_materialization_round_trips_combined_unicode_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = _prepared_entries(tmp_path, monkeypatch)
    entry_count = 2
    entries: list[ArchiveEntry] = []
    for index in range(entry_count):
        prefix = f"U/{index}-"
        emoji = "\N{GRINNING FACE}" * 10
        path = prefix + emoji + "\x01" * (500 - len(prefix) - len(emoji))
        entries.append(ArchiveEntry(path=path, prepared=base.prepared))
    spec = _spec(tuple(entries))
    limits = ArchiveLimits(max_entries=entry_count)

    first = materialize_archive(
        spec,
        tmp_path / "unicode-sidecar",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
        limits=limits,
    )
    sidecar_bytes = first.manifest_path.read_bytes()
    sidecar = json.loads(sidecar_bytes)
    assert sidecar["entries"] == [entry.path for entry in entries]
    assert len(sidecar_bytes) <= archive_module._sidecar_byte_limit(limits)

    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    second = materialize_archive(
        spec,
        tmp_path / "unicode-sidecar",
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(tmp_path, monkeypatch),
        limits=limits,
    )
    assert second.changed is False
    assert ledger.events == []


def test_materialization_noop_preserves_mtimes_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    first = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = {path.name: path.stat().st_mtime_ns for path in first.root.iterdir() if path.is_file()}
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    second = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(tmp_path, monkeypatch),
    )

    assert second.changed is False
    assert ledger.events == []
    assert {
        path.name: path.stat().st_mtime_ns for path in second.root.iterdir() if path.is_file()
    } == before


def test_materialization_noop_rejects_bundle_swap_after_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    real_inspect = archive_module._inspect_bundle_fd

    def swap_after_inspection(*args: object, **kwargs: object) -> ArchiveBuild:
        result = real_inspect(*args, **kwargs)
        (tmp_path / "out").rename(tmp_path / "validated-out")
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "archive.zip").write_bytes(b"unchecked replacement")
        return result

    monkeypatch.setattr(archive_module, "_inspect_bundle_fd", swap_after_inspection)

    with pytest.raises(ArchiveSecurityError, match="Identität|ausgetauscht"):
        materialize_archive(
            spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
            authorizer=_authorizer(tmp_path, monkeypatch),
        )


@pytest.mark.parametrize(
    "corruption",
    ("malformed", "noncanonical", "schema-drift", "wrong-sha", "invalid-zip", "unknown"),
)
def test_materialization_self_heals_invalid_existing_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    first = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    valid_zip = first.zip_path.read_bytes()
    valid_manifest = first.manifest_path.read_bytes()
    manifest = json.loads(valid_manifest)
    if corruption == "malformed":
        first.manifest_path.write_text("{", encoding="utf-8")
    elif corruption == "noncanonical":
        first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif corruption == "schema-drift":
        manifest["schema_version"] = "9.0.0"
        first.manifest_path.write_text(stable_json_dumps(manifest), encoding="utf-8")
    elif corruption == "wrong-sha":
        manifest["output_sha256"] = "a" * 64
        first.manifest_path.write_text(stable_json_dumps(manifest), encoding="utf-8")
    elif corruption == "invalid-zip":
        first.zip_path.write_bytes(b"not a zip")
    else:
        (first.root / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    healed = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(tmp_path, monkeypatch),
    )

    assert healed.changed is True
    assert healed.zip_path.read_bytes() == valid_zip
    assert healed.manifest_path.read_bytes() == valid_manifest
    assert len(ledger.events) == 2


def test_materialization_replaces_zip_and_sidecar_for_changed_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    first = materialize_archive(
        _spec(entries),
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = (first.zip_path.read_bytes(), first.manifest_path.read_bytes())
    changed_spec = _spec((_with_payload(entries[0], b"changed payload"), entries[1]))
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    second = materialize_archive(
        changed_spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(tmp_path, monkeypatch),
    )

    assert second.changed is True
    assert second.zip_path.read_bytes() != before[0]
    assert second.manifest_path.read_bytes() != before[1]
    assert len(ledger.events) == 2


def test_materialization_stage_failure_preserves_old_bundle_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    first = materialize_archive(
        _spec(entries),
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    ledger.record(EffectKind.TEMP_FILE, (tmp_path / "preexisting.tmp").as_posix())
    event_count = len(ledger.events)
    real_build = archive_module.build_archive

    def fail_after_build(*args: object, **kwargs: object) -> ArchiveBuild:
        real_build(*args, **kwargs)
        raise ArchiveIntegrityError("injected stage failure")

    monkeypatch.setattr(archive_module, "build_archive", fail_after_build)

    with pytest.raises(ArchiveIntegrityError, match="injected stage failure"):
        materialize_archive(
            _spec((_with_payload(entries[0], b"changed payload"), entries[1])),
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=_authorizer(tmp_path, monkeypatch),
        )

    assert {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()} == before
    assert len(ledger.events) == event_count


def test_materialization_rejects_target_escape_and_wrong_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    authorizer = _authorizer(tmp_path, monkeypatch)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path / "root")
    with pytest.raises(ArchiveSecurityError, match="temp_root"):
        materialize_archive(
            spec,
            tmp_path / "outside",
            temp_root=tmp_path / "root",
            ledger=ledger,
            authorizer=authorizer,
        )

    with pytest.raises(ValueError, match="Materialize|materialize"):
        materialize_archive(
            spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.PLAN),
            authorizer=authorizer,
        )


def test_materialization_noop_rechecks_current_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    authorizer = _authorizer(tmp_path, monkeypatch)
    first = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=authorizer,
    )
    before = {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
    register = authorizer.authority._register_source
    register.write_text(
        register.read_text(encoding="utf-8").replace("state: approved", "state: takedown"),
        encoding="utf-8",
    )
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    with pytest.raises(ArchiveSecurityError, match="Rechteentscheidung"):
        materialize_archive(
            spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=authorizer,
        )

    assert {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()} == before
    assert ledger.events == []


def test_materialization_rejects_symlinked_existing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    first = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    outside = tmp_path / "outside.json"
    outside.write_text(first.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    first.manifest_path.unlink()
    first.manifest_path.symlink_to(outside)

    with pytest.raises(ArchiveSecurityError, match="Symlink|reguläre Datei"):
        materialize_archive(
            spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
            authorizer=_authorizer(tmp_path, monkeypatch),
        )


def test_materialization_rejects_unexpected_symlink_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_prepared_entries(tmp_path, monkeypatch))
    first = materialize_archive(
        spec,
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    unexpected = first.root / "unexpected"
    unexpected.symlink_to(outside)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    with pytest.raises(ArchiveSecurityError, match="Symlink|reguläre Datei"):
        materialize_archive(
            spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=_authorizer(tmp_path, monkeypatch),
        )

    assert {name: (first.root / name).read_bytes() for name in before} == before
    assert unexpected.is_symlink()
    assert unexpected.readlink() == outside
    assert ledger.events == []
    assert not (tmp_path / ".out.backup").exists()


def test_materialization_second_record_failure_preserves_old_bundle_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    first = materialize_archive(
        _spec(entries),
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    real_record = EffectLedger.record
    calls = 0

    def fail_second_record(
        self: EffectLedger,
        kind: EffectKind,
        target: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
    ) -> object:
        nonlocal calls
        calls += 1
        event = real_record(self, kind, target, sha256=sha256, size=size)
        if calls == 2:
            raise RuntimeError("injected ledger failure")
        return event

    monkeypatch.setattr(EffectLedger, "record", fail_second_record)

    with pytest.raises(RuntimeError, match="injected ledger failure"):
        materialize_archive(
            _spec((_with_payload(entries[0], b"changed payload"), entries[1])),
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=_authorizer(tmp_path, monkeypatch),
        )

    assert {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()} == before
    assert ledger.events == []


def test_materialization_post_commit_cleanup_failure_keeps_new_bundle_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _prepared_entries(tmp_path, monkeypatch)
    first = materialize_archive(
        _spec(entries),
        tmp_path / "out",
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(tmp_path, monkeypatch),
    )
    before = {path.name: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
    changed_spec = _spec((_with_payload(entries[0], b"changed payload"), entries[1]))
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    real_remove_tree_at = staging_module.remove_tree_at

    def fail_backup_cleanup(
        parent_fd: int, name: str, *, require_sentinel: bool = True
    ) -> None:
        if name == ".out.backup":
            raise OSError("injected post-commit cleanup failure")
        real_remove_tree_at(parent_fd, name, require_sentinel=require_sentinel)

    monkeypatch.setattr(staging_module, "remove_tree_at", fail_backup_cleanup)

    with pytest.raises(StagingError, match="sicher veröffentlicht|Backup"):
        materialize_archive(
            changed_spec,
            tmp_path / "out",
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=_authorizer(tmp_path, monkeypatch),
        )

    assert len(ledger.events) == 2
    assert {path.name for path in first.root.iterdir()} == archive_module._BUNDLE_FILES
    assert first.zip_path.read_bytes() != before["archive.zip"]
    sidecar = json.loads(first.manifest_path.read_bytes())
    validate_archive(
        first.zip_path,
        expected_fingerprint=archive_input_fingerprint(changed_spec),
        expected_output_sha256=sidecar["output_sha256"],
    )
    backup = tmp_path / ".out.backup"
    assert {path.name: path.read_bytes() for path in backup.iterdir() if path.is_file()} == before
