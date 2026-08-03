# P07.1 Deterministic Archives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build rights-bound, byte-identical, strictly validated ZIP bundles with atomic materialization and a fixture CLI smoke.

**Architecture:** `archive.py` consumes authorized `PreparedObject` values, computes one canonical input fingerprint, writes fixed-metadata `ZIP_STORED` members, validates without extraction, and publishes ZIP plus archive-manifest as one generated staging bundle. Existing `io_utils`, `staging`, `run_modes`, `schema_registry`, and storage authorization contracts are reused.

**Tech Stack:** Python 3.12 standard library (`zipfile`, `hashlib`, `json`, `os`, `stat`), existing DESINFECT pipeline primitives, pytest, JSON Schema registry.

## Global Constraints

- P07.1 only; no week/month/year scheduling, late-arrival aggregation, reconciliation, repository apply, or backend publication.
- No new dependency or external ZIP executable.
- Output compression is `ZIP_STORED`; changing it requires storage evidence and an archive-format version bump.
- Every input is a `PreparedObject` and receives fresh `RightsStorageAuthorizer` authorization before no-op or build.
- Paths are NFC POSIX, portable-collision-free, non-absolute, traversal-free, and not nested ZIPs.
- ZIP timestamps come from `SOURCE_DATE_EPOCH`, clamp to the ZIP range, and round down to even seconds.
- ZIP contains exactly `MANIFEST.json`, `README.md`, `SHA256SUMS.txt`, and ordered payload members.
- Validation never extracts and enforces entry, member, archive, total-uncompressed, and compression-ratio limits.
- Materialization occurs only beneath explicit `temp_root`; failure preserves previous bundle and ledger state.
- `codegraph`/codex-master-MCP was unavailable; one Context Mode structure/dependency batch is recorded in the Obsidian discovery note.

---

### Task 1: Archive contracts, canonical fingerprint, and metadata

**Files:**
- Create: `scripts/rki_pipeline/archive.py`
- Create: `tests/test_archives.py`

**Interfaces:**
- Consumes: `PreparedObject`, `RightsStorageAuthorizer`, `normalize_posix_path`, `detect_path_collisions`, `stable_json_dumps`.
- Produces: `ArchiveLimits`, `ArchiveEntry`, `ArchiveSpec`, `ArchiveBuild`, `ArchiveInspection`, `ArchiveMaterialization`, `archive_input_fingerprint()`, `_zip_datetime()`.

- [ ] **Step 1: Write failing contract and fingerprint tests**

Create helpers that write payloads below `tmp_path`, build exact `PreparedObject` values with the registered synthetic source `rki:176904/900000001`, and use current rights authority/policy. Add tests:

```python
def test_deterministic_archive_fingerprint_is_order_independent(tmp_path: Path) -> None:
    first, second = _prepared_entries(tmp_path)
    left = _spec((first, second), source_date_epoch=1_700_000_001)
    right = _spec((second, first), source_date_epoch=1_700_000_001)
    assert archive_input_fingerprint(left) == archive_input_fingerprint(right)


def test_archive_fingerprint_binds_identity_timestamp_and_payload(tmp_path: Path) -> None:
    entry, _ = _prepared_entries(tmp_path)
    baseline = archive_input_fingerprint(_spec((entry,), source_date_epoch=1_700_000_001))
    assert archive_input_fingerprint(_spec((entry,), source_date_epoch=1_700_000_003)) != baseline
    assert archive_input_fingerprint(_spec((entry,), period="2026-W02")) != baseline


@pytest.mark.parametrize("epoch, expected", [
    (0, (1980, 1, 1, 0, 0, 0)),
    (1_700_000_001, (2023, 11, 14, 22, 13, 20)),
    (9_999_999_999, (2107, 12, 31, 23, 59, 58)),
])
def test_zip_timestamp_is_clamped_and_even(epoch: int, expected: tuple[int, ...]) -> None:
    assert _zip_datetime(epoch) == expected
```

Also reject wrong dataclass types, duplicate/colliding paths, reserved metadata names, `.zip` members, mixed visibility, count/size limits, and malformed archive IDs/kinds.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_archives.py -k 'fingerprint or timestamp or contract'`  
Expected: collection fails because `scripts.rki_pipeline.archive` does not exist.

- [ ] **Step 3: Implement minimum immutable contracts**

Create:

```python
ARCHIVE_FORMAT_VERSION = "1"
RESERVED_MEMBERS = frozenset({"MANIFEST.json", "README.md", "SHA256SUMS.txt"})
ARCHIVE_KINDS = frozenset({
    "week-pdf", "week-markdown", "month-pdf",
    "month-markdown", "year-pdf", "year-markdown",
})

@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_archive_bytes: int = 4 * 1024 * 1024 * 1024
    max_compression_ratio: int = 100

@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    path: str
    prepared: PreparedObject

@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    archive_id: str
    period: str
    kind: str
    visibility: str
    source_date_epoch: int
    entries: tuple[ArchiveEntry, ...]

@dataclass(frozen=True, slots=True)
class ArchiveBuild:
    path: Path
    input_fingerprint: str
    output_sha256: str
    size: int
    entries: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    path: Path
    input_fingerprint: str
    output_sha256: str
    size: int
    entries: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ArchiveMaterialization:
    root: Path
    zip_path: Path
    manifest_path: Path
    build: ArchiveBuild
    changed: bool
```

Validate exact types in `__post_init__`. Sort entries by canonical path for fingerprints. Compute fingerprint with `stable_json_dumps` over format version, archive identity, normalized ZIP datetime, visibility, and ordered `{path, bytes, sha256}` records.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest -q tests/test_archives.py -k 'fingerprint or timestamp or contract'`  
Expected: all selected tests pass.

- [ ] **Step 5: Commit contracts**

```bash
git add scripts/rki_pipeline/archive.py tests/test_archives.py
git commit -m "feat(p07): define deterministic archive contracts"
```

---

### Task 2: Deterministic renderer and strict validator

**Files:**
- Modify: `scripts/rki_pipeline/archive.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: Task 1 contracts and fingerprint.
- Produces: `build_archive(spec, destination, *, authorizer, limits) -> ArchiveBuild`; `validate_archive(path, *, expected_fingerprint, expected_output_sha256, limits) -> ArchiveInspection`.

- [ ] **Step 1: Write failing determinism and security tests**

Add tests that build two files from the same spec and assert equal bytes/SHA-256. Inspect member order and exact metadata:

```python
def test_deterministic_builds_are_byte_identical(tmp_path: Path) -> None:
    spec = _spec(_prepared_entries(tmp_path), source_date_epoch=1_700_000_001)
    first = build_archive(spec, tmp_path / "first.zip", authorizer=_authorizer())
    second = build_archive(spec, tmp_path / "second.zip", authorizer=_authorizer())
    assert first.output_sha256 == second.output_sha256
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
```

Add `security` tests for traversal, absolute/backslash names, NFC/case collision, symlink source, nested ZIP, stale decision, mixed visibility, encrypted flag, symlink external mode, duplicate member, oversized member/total/archive, high ratio, bad CRC/content SHA, malformed manifest/checksums, and unexpected metadata.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_archives.py -k 'deterministic or security'`  
Expected: failures identify missing renderer/validator.

- [ ] **Step 3: Implement deterministic build**

Implement:

```python
def build_archive(
    spec: ArchiveSpec,
    destination: Path,
    *,
    authorizer: RightsStorageAuthorizer,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveBuild:
    entries = _validated_entries(spec, authorizer=authorizer, limits=limits)
    fingerprint = archive_input_fingerprint(spec)
    manifest = _internal_manifest(spec, entries, fingerprint)
    metadata = {
        "MANIFEST.json": stable_json_dumps(manifest).encode("utf-8"),
        "README.md": _readme(spec, fingerprint).encode("utf-8"),
        "SHA256SUMS.txt": _checksums(entries).encode("utf-8"),
    }
    with ZipFile(destination, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for name in sorted((*metadata, *(entry.path for entry in entries))):
            payload = metadata[name] if name in metadata else _verified_payload(entries_by_path[name])
            archive.writestr(_zip_info(name, spec.source_date_epoch), payload)
    archive_size, output_sha256 = hash_file(destination)
    inspection = validate_archive(
        destination,
        expected_fingerprint=fingerprint,
        expected_output_sha256=output_sha256,
        limits=limits,
    )
    return ArchiveBuild(
        path=destination,
        input_fingerprint=fingerprint,
        output_sha256=output_sha256,
        size=archive_size,
        entries=tuple(entry.path for entry in entries),
    )
```

`_zip_info` fixes `create_system=3`, `compress_type=ZIP_STORED`, `external_attr=(stat.S_IFREG | 0o644) << 16`, empty `extra/comment`, and normalized `date_time`.

- [ ] **Step 4: Implement strict non-extracting validation**

Open with `ZipFile`; bound archive stat size first. Reject unsafe flags, modes, methods, extras/comments, duplicates, directories, nested ZIPs, invalid paths, limit breaches, and excessive ratios before reading. Parse metadata strictly with duplicate-key/nonfinite rejection and canonical JSON comparison. Stream each payload member through SHA-256 and verify CRC/read completion, manifest record, and checksum line.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m pytest -q tests/test_archives.py -k 'deterministic or security'`  
Expected: all selected tests pass.

- [ ] **Step 6: Commit renderer and validator**

```bash
git add scripts/rki_pipeline/archive.py tests/test_archives.py
git commit -m "feat(p07): build and validate deterministic ZIP archives"
```

---

### Task 3: Archive manifest, no-op, and atomic bundle publication

**Files:**
- Modify: `scripts/rki_pipeline/archive.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: Task 2 `build_archive()` and `validate_archive()`, `EffectLedger`, `staged_directory`, `validate_document("archive-manifest", value)`.
- Produces: `materialize_archive(spec, target, *, temp_root, ledger, authorizer, limits) -> ArchiveMaterialization`.

- [ ] **Step 1: Write failing materialization tests**

Add exact tests:

```python
def test_materialization_noop_preserves_mtimes_and_events(tmp_path: Path) -> None:
    spec = _spec(_prepared_entries(tmp_path))
    first = materialize_archive(spec, tmp_path / "out", temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path), authorizer=_authorizer())
    before = {p.name: p.stat().st_mtime_ns for p in first.root.iterdir() if p.is_file()}
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    second = materialize_archive(spec, tmp_path / "out", temp_root=tmp_path,
        ledger=ledger, authorizer=_authorizer())
    assert second.changed is False
    assert ledger.events == []
    assert {p.name: p.stat().st_mtime_ns for p in second.root.iterdir() if p.is_file()} == before
```

Also test invalid existing bundle self-heals, changed input replaces both files, stage failure preserves old bytes and ledger, target escape rejects, wrong ledger mode rejects, and no-op rechecks current rights.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest -q tests/test_archives.py -k 'materialization or noop or rollback'`  
Expected: missing `materialize_archive` failures.

- [ ] **Step 3: Implement sidecar and strict loader**

Render schema-compatible sidecar:

```python
{
    "schema_version": "1.0.0",
    "archive_id": spec.archive_id,
    "period": spec.period,
    "kind": spec.kind,
    "entries": [entry.path for entry in ordered_entries],
    "input_fingerprint": build.input_fingerprint,
    "output_sha256": build.output_sha256,
    "storage_reference": None,
}
```

Use canonical `stable_json_dumps`; strict load rejects symlinks, unknown files, malformed JSON, schema drift, wrong archive SHA, and invalid ZIP.

- [ ] **Step 4: Implement staged publication and no-op**

Require exact materialize ledger/temp root. Reauthorize before reading existing output. Return no-op only after sidecar, hash, and ZIP all validate against the expected fingerprint. Otherwise build in `staged_directory(target, allowed_root=temp_root, replace_existing=True)`, validate staged pair, publish, then record two `TEMP_FILE` events. On error restore ledger length and let staging rollback.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m pytest -q tests/test_archives.py -k 'materialization or noop or rollback'`  
Expected: all selected tests pass.

- [ ] **Step 6: Commit atomic materialization**

```bash
git add scripts/rki_pipeline/archive.py tests/test_archives.py
git commit -m "feat(p07): atomically materialize archive bundles"
```

---

### Task 4: CLI smoke, operations documentation, CI, and delivery gate

**Files:**
- Modify: `scripts/rki_pipeline/cli.py`
- Modify: `scripts/rki_pipeline/archive.py`
- Modify: `tests/test_archives.py`
- Create: `docs/Wartung/Archivformat.md`
- Modify: `.github/workflows/p00-baseline.yml`
- Modify: `.github/workflows/rki-pipeline.yml`

**Interfaces:**
- Consumes: Task 3 materializer.
- Produces: `archive.main(argv) -> int`; root CLI command `build-archive`.

- [ ] **Step 1: Write failing CLI tests**

Add tests for exact blueprint command, unsupported mode, unknown fixture, stable JSON result, temporary cleanup, and unchanged repository snapshot:

```python
def test_build_archive_pilot_cli_is_offline_and_deterministic(capsys) -> None:
    assert pipeline_cli.main(["build-archive", "--fixture", "pilot", "--mode", "materialize"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert len(payload["input_fingerprint"]) == 64
    assert len(payload["output_sha256"]) == 64
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `python3 -m pytest -q tests/test_archives.py -k cli`  
Expected: root router prints current convert-only usage and returns 2.

- [ ] **Step 3: Implement thin fixture CLI and router**

Route `build-archive` to `archive.main`. Accept only `--fixture pilot --mode materialize`. Use `TemporaryDirectory`, create one synthetic authorized `PreparedObject`, materialize and validate, print canonical JSON with changed/input/output SHA and byte count, then clean the temporary directory. Never write repository files.

- [ ] **Step 4: Document exact archive format and operations**

Document fixed metadata, `ZIP_STORED` rationale/ceiling, internal files, fingerprint fields, limits, validation, no-op, rollback, CLI smoke, and P07.2 ownership of repository/backend publication in `docs/Wartung/Archivformat.md`.

- [ ] **Step 5: Add focused CI gates**

Add commands to both workflows:

```yaml
- name: Validate deterministic archive contracts
  run: |
    python -m pytest -q tests/test_archives.py
    python -m scripts.rki_pipeline.cli build-archive --fixture pilot --mode materialize
```

- [ ] **Step 6: Run complete local verification**

Run:

```bash
python3 -m pytest -q tests/test_archives.py
python3 -m scripts.rki_pipeline.cli build-archive --fixture pilot --mode materialize
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 scripts/validate_p04_storage.py
python3 scripts/validate_p05_dispatcher.py
python3 scripts/validate_rights_register.py
python3 scripts/validate_manifests.py --root tests/fixtures/manifests
python3 scripts/validate_ci_mutation_safety.py
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
ruff check scripts tests
npm test
git diff --check
```

Expected: every command exits 0; two deterministic builds have identical SHA-256; no working-tree file beyond planned changes appears.

- [ ] **Step 7: Commit delivery integration**

```bash
git add scripts/rki_pipeline/archive.py scripts/rki_pipeline/cli.py tests/test_archives.py docs/Wartung/Archivformat.md .github/workflows/p00-baseline.yml .github/workflows/rki-pipeline.yml
git commit -m "feat(p07): expose deterministic archive builder"
```

- [ ] **Step 8: Review and publish**

Run independent spec-compliance and code-quality/security reviews. Fix validated findings with focused tests. Push branch, open PR, wait for GitHub Actions/CodeRabbit/qlty, resolve every actionable thread, squash-merge, wait for post-merge CI, then update P07.1 blueprint and machine status in a separate governance closeout.

## Plan self-review

- Spec coverage: contracts, rights, deterministic metadata, internal files, security limits, validation, no-op, rollback, CLI, docs, CI, and scope boundary each map to a task.
- Placeholder scan: no incomplete implementation instruction remains.
- Type consistency: Task 1 types feed Tasks 2–4 without renaming.
- Scope: P07.2 scheduling/period outputs and P07.3 reconciliation are explicitly excluded.
