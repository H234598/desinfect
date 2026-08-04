# P07.3 Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic quarterly reconciliation that detects remote, local, storage, rights, and period/archive drift without blind downloads or success-watermark mutation on unresolved findings.

**Architecture:** Add one manifest-first reconciliation domain module. It consumes a validated manifest catalog, remote metadata snapshot, existing storage adapters, rights authority, and public P07 period inspection; each authority produces deterministic findings that compose into the existing schema-valid 1.0.0 aggregate report. Materialization is temp-root-only and writes an immutable report only on success.

**Tech Stack:** Python 3.12 standard library, existing `jsonschema`, pytest, P04 storage adapters, P06 manifests/rights, P07 archive/aggregation validators, existing EffectLedger/IO primitives.

## Global Constraints

- Scope is P07.3 only; no backfill, repair, readiness gate, direct runtime-status edit, repository apply, commit, push, or new dependency.
- Reuse `LoadedManifestCatalog`, `StorageAdapter`, `resolve_rights`, `validate_period_manifest`, and archive-bundle validation.
- Remote metadata joins current non-superseded local sources by exact `(source_id, bitstream_id)`.
- Candidate content loads occur only after version, identity, URL, ETag, Last-Modified, or supplied-hash drift.
- Finding order is code, subject kind, subject ID, then canonical relative path.
- Detail `new` increments report `missing_local`; every non-`ok` detail increments `unresolved`.
- Existing `reconciliation-report` schema stays at `1.0.0`; P07.3 emits only `success` or `blocked`.
- Blocked results have `successful_at=None`, write no canonical report, and leave ledger without persistent effects.
- Success report path is `rki/Bulletins/Manifeste/Reconciliation/reconciliation-YYYYMMDDTHHMMSSZ.json`.
- CLI fixture is offline, deterministic, bounded, symlink-safe, and accepts only lowercase `plan|materialize`; `apply` is rejected.
- Error output is path-free and contains no payload bytes, response bodies, tokens, or absolute paths.

---

### Task 1: Reconciliation domain model and report contract

**Files:**
- Create: `scripts/rki_pipeline/reconciliation.py`
- Create: `tests/test_reconciliation.py`
- Modify: `tests/test_schemas.py`

**Interfaces:**
- Consumes: `LoadedManifestCatalog`, `validate_document("reconciliation-report", payload)`, `stable_json_dumps`, aware UTC `datetime`.
- Produces:
  - `FindingCode(StrEnum)`
  - `SubjectKind(StrEnum)`
  - `ReconciliationFinding(code, subject_kind, subject_id, relative_path, message)`
  - `ReconciliationCounts(ok, changed, missing_remote, missing_local, orphan, rights_changed, unresolved)`
  - `ReconciliationResult(findings, counts, conclusion, source_manifest_sha256, report, successful_at)`
  - `source_subject_id(source_id: str, bitstream_id: str)->str`
  - `build_reconciliation_result(...)->ReconciliationResult`

- [ ] **Step 1: Write failing model and count tests**

Add exact tests:

```python
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
```

Also reject boolean sizes, uppercase hashes, duplicate finding keys, naive/non-UTC timestamps, reversed/out-of-range years, `ok` mixed with non-`ok` for one source, control characters, absolute paths, and messages longer than 500 characters.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "result or finding or candidate"
```

Expected: import failure for `scripts.rki_pipeline.reconciliation`.

- [ ] **Step 3: Implement exact minimal domain**

Use these signatures:

```python
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

    def to_dict(self) -> dict[str, int]: ...


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
) -> ReconciliationResult: ...
```

Derive counts in one pass. Map `NEW` to `missing_local`. Build UTC text with `as_of.isoformat().replace("+00:00", "Z")`. Validate report through `validate_document` before constructing the result. Every known source/bitstream-related finding uses `source_subject_id`; orphan-only findings use their artifact ID.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "result or finding or candidate"
python3 scripts/validate_schemas.py
ruff check scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py
```

Expected: all pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py tests/test_schemas.py
git commit -m "feat(p07): define reconciliation report contract"
```

### Task 2: Remote metadata comparison and bounded candidate loading

**Files:**
- Modify: `scripts/rki_pipeline/reconciliation.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Consumes: `LoadedManifestCatalog.graph`, `ArtifactRecord`, `PreparedObject`.
- Produces:
  - `CandidateLoader = Callable[[ArtifactRecord], PreparedObject]`
  - `compare_remote_sources(catalog, remote_records, candidate_loader=None)->tuple[ReconciliationFinding, ...]`
  - private current-source projection keyed by `(source_id, bitstream_id)`.

Exact comparison signature:

```python
CandidateLoader: TypeAlias = Callable[[ArtifactRecord], PreparedObject]


def compare_remote_sources(
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    *,
    candidate_loader: CandidateLoader | None = None,
) -> tuple[ReconciliationFinding, ...]: ...
```

- [ ] **Step 1: Write failing no-blind-download and drift tests**

Build one validated catalog fixture with a current non-superseded document. Add a spy loader:

```python
calls: list[str] = []

def loader(record: ArtifactRecord) -> PreparedObject:
    calls.append(record.source_id)
    return candidate_prepared_object(tmp_path, sha256=LOCAL_SOURCE_SHA256)

findings = compare_remote_sources(
    catalog,
    (matching_remote_record(),),
    candidate_loader=loader,
)
assert calls == []
assert findings == ()

changed = replace(matching_remote_record(), etag='"new"')
findings = compare_remote_sources(catalog, (changed,), candidate_loader=loader)
assert calls == [changed.source_id]
assert [item.code for item in findings] == [FindingCode.CHANGED]
```

Add cases for remote-only `new`, local-only `missing_remote`, duplicate remote `(source_id, bitstream_id)`, changed URL/version/Last-Modified/supplied hash, same candidate hash, changed candidate hash, missing loader, loader error, and superseded local source ignored.

- [ ] **Step 2: Run remote tests and confirm RED**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "remote or candidate or blind or superseded"
```

Expected: `compare_remote_sources` missing.

- [ ] **Step 3: Implement metadata-first comparison**

Derive remote bitstream identity only from `record.pdf_url` with existing `bitstream_identity`. Reject records without a canonical PDF identity when they claim downloadable content.

Project local current sources by joining document `superseded_by is None` to source `(source_id, bitstream_id)`. Compare:

```python
_METADATA_FIELDS = (
    ("version", "version"),
    ("source_url", "item_url"),
    ("bitstream_url", "pdf_url"),
    ("etag", "etag"),
    ("last_modified", "last_modified"),
    ("publication_date", "publication_date"),
)
```

Only byte-relevant drift on known records calls the loader: version, source or bitstream URL, bitstream identity, ETag, Last-Modified, or supplied SHA-256. Publication-date and rights-evidence drift never loads candidate bytes. Validate returned `PreparedObject.source_id`, `source_sha256`, `document_id`, size, hash, and temp-root containment through its existing type contract. Always retain `changed` for metadata drift even when candidate bytes match.

Use bounded generic messages such as `"Remote-Metadaten driften"`; never include URL or loader exception text.

- [ ] **Step 4: Run remote and model tests**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "remote or candidate or blind or superseded or result"
ruff check scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py
```

Expected: all pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py
git commit -m "feat(p07): compare remote source metadata"
```

### Task 3: Storage and rights reconciliation through existing contracts

**Files:**
- Modify: `scripts/rki_pipeline/manifests.py:302-320`
- Modify: `scripts/rki_pipeline/reconciliation.py`
- Modify: `tests/test_manifests.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Consumes: manifest storage dictionaries, `StorageAdapter.verify`, `StorageAdapter.list_references`, `resolve_rights`.
- Produces:
  - `storage_reference_from_manifest(value: Mapping[str, object])->StorageReference`
  - `reconcile_storage(graph, adapters)->tuple[ReconciliationFinding, ...]`
  - `reconcile_rights(graph, authority, policy)->tuple[ReconciliationFinding, ...]`.

Exact check signatures:

```python
def reconcile_storage(
    graph: ManifestGraph,
    adapters: Mapping[StorageBackend, StorageAdapter],
) -> tuple[ReconciliationFinding, ...]: ...


def reconcile_rights(
    graph: ManifestGraph,
    *,
    authority: RightsAuthority,
    policy: RightsPolicy,
) -> tuple[ReconciliationFinding, ...]: ...
```

- [ ] **Step 1: Write failing public conversion test**

Rename no behavior yet in the test:

```python
reference = storage_reference_from_manifest(valid_storage_manifest())
assert reference.to_dict() == valid_storage_manifest()
```

Retain existing graph validation assertions. Add rejection for wrong backend, hash, byte count, provenance linkage, and unknown keys through existing schema/type validation.

- [ ] **Step 2: Run conversion test and confirm RED**

Run:

```bash
pytest -q tests/test_manifests.py -k storage_reference_from_manifest
```

Expected: public function missing.

- [ ] **Step 3: Expose existing conversion without duplicating it**

Rename `_storage_reference` to `storage_reference_from_manifest` and update `_validate_storage` to call the public name. Do not change construction or validation semantics.

- [ ] **Step 4: Write failing storage/rights tests**

Use a tiny fake adapter implementing the exact protocol:

```python
@dataclass
class RecordingAdapter:
    backend: StorageBackend
    references: tuple[StorageReference, ...]
    failures: dict[str, Exception]
    verified: list[str] = field(default_factory=list)

    def verify(self, reference: StorageReference) -> None:
        self.verified.append(reference.artifact_id)
        failure = self.failures.get(reference.artifact_id)
        if failure is not None:
            raise failure

    def list_references(self) -> tuple[StorageReference, ...]:
        return self.references
```

Stub unrelated protocol methods with `AssertionError`; reconciliation must never call them.

Cover:

- every graph reference verified once by matching backend;
- missing adapter and adapter `FileNotFoundError` → `missing_local`;
- hash/pointer/integrity error → `changed`;
- adapter inventory extra reference → `orphan`;
- duplicate artifact/reference identity fails closed;
- persisted decision hash/current decision mismatch → `rights_changed`;
- restricted/takedown/metadata-only decision → `rights_changed`;
- unchanged approved decision → no finding;
- error messages contain no absolute path or exception payload.

- [ ] **Step 5: Implement storage and rights checks**

Storage pseudocode:

```python
for manifest in sorted(graph.storage_references, key=lambda item: item["artifact_id"]):
    reference = storage_reference_from_manifest(manifest)
    adapter = adapters.get(reference.storage_backend)
    if adapter is None:
        add(MISSING_LOCAL, reference)
        continue
    try:
        adapter.verify(reference)
    except FileNotFoundError:
        add(MISSING_LOCAL, reference)
    except StorageAuthorizationError:
        add(RIGHTS_CHANGED, reference)
    except StorageError:
        add(CHANGED, reference)

for reference in sorted(adapter.list_references(), key=lambda item: item.artifact_id):
    if reference.artifact_id not in manifest_artifact_ids:
        add(ORPHAN, reference)
```

Rights check resolves each current source exact `(source_id, sha256)` and compares decision hash/state against source and every linked storage reference. Catch rights contract errors as `ReconciliationIntegrityError`; expected changed decisions become findings.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "storage or orphan or rights"
pytest -q tests/test_manifests.py tests/test_rights_policy.py tests/test_storage_contract.py tests/test_storage_lfs.py
ruff check scripts/rki_pipeline/manifests.py scripts/rki_pipeline/reconciliation.py tests/test_manifests.py tests/test_reconciliation.py
```

Expected: all pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add scripts/rki_pipeline/manifests.py scripts/rki_pipeline/reconciliation.py tests/test_manifests.py tests/test_reconciliation.py
git commit -m "feat(p07): reconcile storage and rights"
```

### Task 4: Public period publication inspection and completeness findings

**Files:**
- Modify: `scripts/rki_pipeline/aggregation.py`
- Modify: `scripts/rki_pipeline/reconciliation.py`
- Modify: `tests/test_period_archives.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Consumes: `PeriodRef`, period root, period-manifest schema, existing archive-bundle descriptor validation.
- Produces:
  - `PeriodPublicationInspection(period, manifest, manifest_sha256)`
  - `inspect_period_publication(root: Path, period: PeriodRef)->PeriodPublicationInspection`
  - `reconcile_periods(graph, period_root)->tuple[ReconciliationFinding, ...]`.

Exact completeness signature:

```python
def reconcile_periods(
    graph: ManifestGraph,
    period_root: Path,
) -> tuple[ReconciliationFinding, ...]: ...
```

- [ ] **Step 1: Write failing P07 authority tests**

In `tests/test_period_archives.py`, materialize a known plan, then:

```python
inspection = inspect_period_publication(result.root, period_ref(TaskKind.WEEK, "2025-W50"))
assert inspection.period.value == "2025-W50"
assert inspection.manifest["kind"] == "week"
assert inspection.manifest_sha256 == hashlib.sha256(
    stable_json_dumps(inspection.manifest) + b"\n"
).hexdigest()
```

Corrupt one ZIP, sidecar, manifest hash, manifest period, and symlinked component separately. Expect `PeriodManifestError` or `ArchiveError`. Remove the period manifest and expect `FileNotFoundError`.

- [ ] **Step 2: Run inspection tests and confirm RED**

Run:

```bash
pytest -q tests/test_period_archives.py -k inspect_period_publication
```

Expected: public inspection interface missing.

- [ ] **Step 3: Implement inspection by extracting existing safe reads**

Define the exact result type:

```python
@dataclass(frozen=True, slots=True)
class PeriodPublicationInspection:
    period: PeriodRef
    manifest: dict[str, object]
    manifest_sha256: str
```

`inspect_period_publication` must:

1. open the root and nested manifest path with existing dir-fd helpers and no symlink following;
2. read bounded stable bytes;
3. call `validate_period_manifest`;
4. confirm manifest kind/value equals requested `PeriodRef`;
5. for every archive entry, open `relative_bundle` safely and call existing archive bundle validator with exact sidecar/output identities;
6. return a deep-copied manifest and SHA-256 of canonical manifest bytes.

Do not duplicate ZIP parsing, local-header checks, size limits, or fingerprint checks.

- [ ] **Step 4: Write failing completeness tests**

Build graph documents for:

- full date requiring week/month/year;
- year-only record requiring year only;
- PDF-only, Markdown-only, and both formats.

Assert:

- all three complete manifests → no finding;
- missing period manifest/bundle → `missing_local`;
- corrupt or mismatched manifest/bundle → `changed`;
- missing unavailable format is not a finding;
- wrong document/bitstream membership → `changed`;
- invalid month-to-week/year-to-month link → `changed`;
- findings use canonical period IDs, not absolute paths.

- [ ] **Step 5: Implement `reconcile_periods`**

Derive expected periods from each current non-superseded document:

```python
published = date.fromisoformat(document["publication_date"])
iso = published.isocalendar()
expected = (
    period_ref(TaskKind.WEEK, f"{iso.year:04d}-W{iso.week:02d}"),
    period_ref(TaskKind.MONTH, f"{published.year:04d}-{published.month:02d}"),
    period_ref(TaskKind.YEAR, f"{published.year:04d}"),
)
```

For nullable full date but known year, verify year only. Cache one inspection per `(kind, value)`. Compare document/bitstream membership and non-null artifact formats to manifest document rows. Map absence/corruption without exposing exception text.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "period or archive or completeness"
pytest -q tests/test_period_archives.py tests/test_archives.py
ruff check scripts/rki_pipeline/aggregation.py scripts/rki_pipeline/reconciliation.py tests/test_period_archives.py tests/test_reconciliation.py
```

Expected: all pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add scripts/rki_pipeline/aggregation.py scripts/rki_pipeline/reconciliation.py tests/test_period_archives.py tests/test_reconciliation.py
git commit -m "feat(p07): reconcile period publications"
```

### Task 5: Compose plan and materialize successful immutable reports

**Files:**
- Modify: `scripts/rki_pipeline/reconciliation.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Consumes: remote/storage/rights/period finding functions, `EffectLedger`, `RunMode`, `atomic_write_bytes`.
- Produces:
  - `plan_reconciliation(...)->ReconciliationResult`
  - `ReconciliationMaterialization(result, path, changed)`
  - `materialize_reconciliation(result, temp_root, ledger)->ReconciliationMaterialization`.

Exact composition signatures:

```python
def plan_reconciliation(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    adapters: Mapping[StorageBackend, StorageAdapter],
    period_root: Path,
    authority: RightsAuthority,
    policy: RightsPolicy,
    candidate_loader: CandidateLoader | None = None,
) -> ReconciliationResult: ...


def materialize_reconciliation(
    result: ReconciliationResult,
    *,
    temp_root: Path,
    ledger: EffectLedger,
) -> ReconciliationMaterialization: ...
```

- [ ] **Step 1: Write failing composition tests**

Create one fully consistent fixture. Assert:

```python
result = plan_reconciliation(
    as_of=AS_OF,
    from_year=1996,
    to_year=1996,
    catalog=catalog,
    remote_records=remote_records,
    adapters=adapters,
    period_root=period_root,
    authority=authority,
    policy=policy,
)
assert result.conclusion == "success"
assert result.counts.unresolved == 0
assert result.successful_at == AS_OF
assert result.report["source_manifest_sha256"] == hashlib.sha256(
    dict(catalog.rendered.files)["Quellen/manifest.jsonl"]
).hexdigest()
```

Add a single finding from each component and assert order, counts, `blocked`, and `successful_at is None`. Add an exact duplicate finding key from two components and assert fail-closed rejection.

- [ ] **Step 2: Run composition tests and confirm RED**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "plan_reconciliation or composed"
```

Expected: composition function missing.

- [ ] **Step 3: Implement composition and source-manifest hash**

Call components in fixed order, then sort by finding key. If no non-`ok` finding exists with a current `source_subject_id`, add exactly one source-level `ok` for that source/bitstream. Hash exact rendered `Quellen/manifest.jsonl` bytes. Reject a missing or duplicate rendered file name.

- [ ] **Step 4: Write failing materialization tests**

Cover:

- success writes exact compact UTC path below temp root;
- report validates and bytes equal `stable_json_dumps(result.report) + b"\n"`;
- same result/timestamp preserves mtime and emits no new ledger event;
- blocked result writes no file and no event;
- existing different bytes at same immutable path fail closed;
- injected atomic-write error leaves old reports and ledger unchanged;
- `PLAN` and `APPLY` ledgers are rejected;
- mismatched `temp_root` is rejected;
- symlinked report directory is rejected.

- [ ] **Step 5: Implement materialization**

Use:

```python
@dataclass(frozen=True, slots=True)
class ReconciliationMaterialization:
    result: ReconciliationResult
    path: Path | None
    changed: bool
```

For blocked results return `path=None, changed=False`. For success, validate exact `MATERIALIZE` mode/root, derive `YYYYMMDDTHHMMSSZ`, compare any existing regular file byte-for-byte, and atomically write only a new path. Record `EffectKind.TEMP_FILE` after successful write.

- [ ] **Step 6: Run Task 5 tests**

Run:

```bash
pytest -q tests/test_reconciliation.py -k "plan_reconciliation or composed or materialize or watermark or rollback"
python3 scripts/validate_write_policy.py
ruff check scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py
```

Expected: all pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add scripts/rki_pipeline/reconciliation.py tests/test_reconciliation.py
git commit -m "feat(p07): materialize reconciliation reports"
```

### Task 6: Offline fixture CLI and domain router

**Files:**
- Modify: `scripts/rki_pipeline/reconciliation.py`
- Modify: `scripts/rki_pipeline/cli.py`
- Create: `tests/fixtures/reconciliation/fixture.json`
- Create: `tests/test_reconciliation_cli.py`
- Modify: `tests/test_pipeline_cli.py`

**Interfaces:**
- Consumes: strict fixture directory, `plan_reconciliation`, `materialize_reconciliation`.
- Produces:
  - `reconciliation.main(argv: list[str] | None = None)->int`
  - router command `reconcile`.

- [ ] **Step 1: Add strict minimal fixture definition**

`fixture.json` contains only canonical JSON fields:

```json
{
  "schema_version": "1.0.0",
  "as_of": "2026-08-04T04:00:00Z",
  "scope": {"from_year": 2025, "to_year": 2025},
  "source": {
    "handle": "176904/900000001",
    "publication_date": "2025-12-12",
    "title": "Synthetic reconciliation bulletin",
    "pdf_url": "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=1"
  },
  "payload": "# Synthetic deterministic reconciliation fixture\n"
}
```

Fixture loader derives hashes and IDs, creates bytes only inside one `TemporaryDirectory`, builds valid source/document/storage manifests through existing builders, materializes week/month/year products through P07.2, and builds matching remote `ArtifactRecord`. Unknown keys, oversized file, symlink, noncanonical URL, or mismatched fixed `as_of` fails.

- [ ] **Step 2: Write failing CLI tests**

Run the module twice for each mode with hostile environment changes:

```python
first = run_cli("plan", env={"TZ": "Pacific/Kiritimati", "SOURCE_DATE_EPOCH": "1"})
second = run_cli("plan", env={"TZ": "UTC", "SOURCE_DATE_EPOCH": "9999999999"})
assert first.returncode == second.returncode == 0
assert first.stdout == second.stdout
assert first.stderr == second.stderr == b""
```

Also cover:

- exact blueprint command in `materialize`;
- output schema/counts/conclusion/report path;
- missing/unknown/uppercase mode and `apply` return 2 without traceback;
- malformed/oversized/symlinked fixture returns 1 with fixed path-free stderr;
- fixture directory and repository snapshots unchanged after both modes;
- existing `convert|build-archive|aggregate` routing unchanged.

- [ ] **Step 3: Run CLI tests and confirm RED**

Run:

```bash
pytest -q tests/test_reconciliation_cli.py tests/test_pipeline_cli.py
```

Expected: `reconcile` router/CLI missing.

- [ ] **Step 4: Implement parser, fixture loader, and stable evidence**

Parser:

```python
parser = argparse.ArgumentParser(prog="reconcile")
parser.add_argument("--fixture", required=True)
parser.add_argument("--mode", choices=("plan", "materialize"), required=True)
```

Output one `stable_json_dumps` line containing mode, conclusion, counts, source-manifest hash, deterministic findings, nullable report path, and changed flag. Catch only expected reconciliation/schema/storage/rights/archive/OS errors. Print `"reconcile: fixture validation failed"` or `"reconcile: reconciliation failed"` without exception text.

Router usage becomes:

```text
usage: python -m scripts.rki_pipeline.cli (convert|build-archive|aggregate|reconcile) ...
```

- [ ] **Step 5: Run CLI and regression tests**

Run:

```bash
pytest -q tests/test_reconciliation_cli.py tests/test_pipeline_cli.py
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
python3 scripts/validate_ci_mutation_safety.py
ruff check scripts/rki_pipeline/cli.py scripts/rki_pipeline/reconciliation.py tests/test_reconciliation_cli.py tests/test_pipeline_cli.py
```

Expected: all pass; both commands exit 0.

- [ ] **Step 6: Commit Task 6**

```bash
git add scripts/rki_pipeline/reconciliation.py scripts/rki_pipeline/cli.py tests/fixtures/reconciliation/fixture.json tests/test_reconciliation_cli.py tests/test_pipeline_cli.py
git commit -m "feat(p07): add offline reconciliation CLI"
```

### Task 7: Operations documentation, CI gate, and full verification

**Files:**
- Create: `docs/Wartung/Reconciliation.md`
- Create: `runbooks/RKI-SOURCE-CHANGED.md`
- Modify: `.github/workflows/p00-baseline.yml`
- Modify: `scripts/validate_all_baseline.py`
- Modify: `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md` only after feature merge, in separate governance PR
- Modify: `docs/implementation-status.json` only after feature merge, in separate governance PR
- Modify: `config/plan-source.json` only after steering-file closeout change

**Interfaces:**
- Consumes: final CLI and finding contracts.
- Produces: operator procedure and CI evidence; no new runtime interface.

- [ ] **Step 1: Write operations documentation**

`docs/Wartung/Reconciliation.md` must state:

- authorities and exact comparison order;
- every finding code and `new -> missing_local` count mapping;
- candidate-load conditions and no-blind-download proof;
- storage/LFS, rights, week/month/year completeness checks;
- report path, schema, immutable history, and source-manifest hash;
- `success`/nullable watermark contract;
- blocked diagnosis and rollback;
- exact plan/materialize commands;
- explicit non-goals: no repair, apply, backfill, or readiness.

`runbooks/RKI-SOURCE-CHANGED.md` must contain ordered safe triage:

1. preserve report, source manifest, and remote metadata evidence;
2. identify finding code/subject without downloading blindly;
3. for `changed`, rerun bounded candidate materialization;
4. for rights drift, stop publication and review canonical register;
5. for storage/archive drift, verify adapter/object and regenerate through P05/P07, never edit ZIP/manifest manually;
6. rerun plan then materialize;
7. advance watermark only after `success`;
8. escalate unresolved remote deletion/orphan rather than auto-delete.

- [ ] **Step 2: Add focused CI commands**

Add to existing baseline job, preserving read-only mutation guard:

```yaml
- name: P07.3 reconciliation tests
  run: pytest -q tests/test_reconciliation.py tests/test_reconciliation_cli.py

- name: P07.3 offline reconciliation smoke
  run: |
    python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
    python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
```

Extend baseline validator expected path sets for the module, tests, fixture, docs, and runbook. Do not add network, credentials, writable repository mode, or new action.

- [ ] **Step 3: Run focused P07.3 gate**

Run:

```bash
pytest -q tests/test_reconciliation.py tests/test_reconciliation_cli.py tests/test_period_archives.py tests/test_archives.py tests/test_manifests.py tests/test_rights_policy.py tests/test_storage_contract.py tests/test_storage_lfs.py
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
python3 scripts/validate_all_baseline.py
python3 scripts/validate_ci_mutation_safety.py
python3 scripts/validate_schemas.py
python3 scripts/validate_write_policy.py
```

Expected: all pass.

- [ ] **Step 4: Run complete repository gate**

Run:

```bash
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
ruff check scripts tests
git diff --check
```

Expected: all pass and worktree contains only intended P07.3 files.

- [ ] **Step 5: Commit Task 7**

```bash
git add docs/Wartung/Reconciliation.md runbooks/RKI-SOURCE-CHANGED.md .github/workflows/p00-baseline.yml scripts/validate_all_baseline.py
git commit -m "docs(p07): document reconciliation operations"
```

- [ ] **Step 6: Request two-stage review and correct findings**

Dispatch one spec-compliance reviewer and one code-quality/security reviewer. Fix only independently validated findings. Repeat focused/full gates after every correction batch. Resolve every actionable PR thread before merge.

- [ ] **Step 7: Merge feature PR and close governance separately**

After feature PR checks are green and expected HEAD is unchanged:

1. squash merge P07.3 feature PR;
2. verify post-merge Actions on `main`;
3. create a fresh closeout branch from `origin/main`;
4. mark only P07.3 `umgesetzt`, update 27/60 progress, record exact feature PR/SHA/check/test/requirement evidence, advance next phase to P08.1, and update canonical steering hash;
5. run governance validators, open closeout PR, resolve review, squash merge, and verify post-merge Actions;
6. synchronize Obsidian live plan with both merges and exact gates.
