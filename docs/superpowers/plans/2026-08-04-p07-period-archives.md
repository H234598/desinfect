# P07.2 Period Archives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate deterministic weekly, monthly, and yearly PDF/Markdown archive products and rebuild exactly the closed historical periods named by late-arrival metadata.

**Architecture:** `aggregation.py` is a pure planning and rendering layer above P05 `DueTask`, grabber `AffectedPeriods`, P06 `ManifestGraph`, P04 `PreparedObject`, and P07.1 `ArchiveSpec`/`materialize_archive`. One new registered period-manifest schema records document versions, archive identities, checksums, month links, and backend-neutral artifact references without changing the P07.1 sidecar.

**Tech Stack:** Python 3.12 stdlib (`dataclasses`, `datetime`, `enum`, `hashlib`, `json`, `pathlib`, `zoneinfo`), existing jsonschema registry, pytest, existing atomic staging and rights/storage contracts.

## Global Constraints

- Scope is P07.2 only: `V2-08-WEEK-001..004`, `V2-09-MONTH-001`, `V2-10-YEAR-001`, `V2-11-ARCHIVE-001`, `MUSS-19`, `MUSS-20`, `MUSS-21`, `MUSS-31`; P07.2 produces `MUSS-21` evidence while P07.3 performs quarterly reconciliation.
- Reuse P05 due tasks, grabber `AffectedPeriods`, P06 manifest graph, P04 prepared objects, and P07.1 archive builder; no second ZIP implementation.
- Period timezone is exactly `Europe/Berlin`; archive timestamp is period end-exclusive midnight converted to UTC.
- Only fully closed periods are accepted. Due and affected values are validated, deduplicated, and sorted.
- P07.2 supports `plan` and isolated `materialize`; direct `apply` is rejected.
- No archive contains another ZIP. No empty format archive is emitted.
- One archive has one visibility. Mixed visibility for the same period/format fails closed instead of changing required names.
- All writes remain below explicit `temp_root`; P05 owns repository apply, commit, and push.
- No new dependency.

---

### Task 1: Period and schema contracts

**Files:**
- Create: `scripts/rki_pipeline/aggregation.py`
- Create: `tests/test_period_archives.py`
- Create: `schemas/period-archive-manifest.schema.json`
- Create: `tests/fixtures/schemas/period-archive-manifest.json`
- Modify: `config/schema-registry.json`
- Modify: `scripts/validate_schemas.py:24-70`
- Modify: `tests/test_schemas.py:78-86`

**Interfaces:**
- Consumes: `TaskKind`, `DueTask`, `AffectedPeriods`, aware `datetime`.
- Produces: `PeriodRef`, `period_ref(kind, value)`, `select_periods(as_of, due_tasks, affected_periods)` and registered schema name `period-archive-manifest` version `1.0.0`.

- [ ] **Step 1: Write failing closed-period and union tests**

```python
from datetime import date, datetime, timezone

import pytest

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline.aggregation import PeriodSelectionError, period_ref, select_periods
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind


def due(kind: TaskKind, period: str) -> DueTask:
    return DueTask(
        task_id=f"{kind.value}:{period}",
        kind=kind,
        period=period,
        reason="test",
        due_at="2026-01-01T05:00:00Z",
    )


def test_berlin_period_boundaries_have_stable_epochs() -> None:
    week = period_ref(TaskKind.WEEK, "2025-W52")
    month = period_ref(TaskKind.MONTH, "2026-07")
    year = period_ref(TaskKind.YEAR, "2025")
    assert (week.start, week.end, week.source_date_epoch) == (
        date(2025, 12, 22), date(2025, 12, 28), 1766962800
    )
    assert (month.start, month.end, month.source_date_epoch) == (
        date(2026, 7, 1), date(2026, 7, 31), 1785535200
    )
    assert (year.start, year.end, year.source_date_epoch) == (
        date(2025, 1, 1), date(2025, 12, 31), 1767222000
    )


def test_due_and_affected_periods_are_unioned_once_and_sorted() -> None:
    affected = AffectedPeriods(
        weeks={"2025-W52", "2025-W50"},
        months={"2025-12"},
        years={2025},
    )
    periods = select_periods(
        datetime(2026, 1, 5, 5, tzinfo=timezone.utc),
        (due(TaskKind.WEEK, "2025-W52"), due(TaskKind.MONTH, "2025-12")),
        affected,
    )
    assert tuple((item.kind.value, item.value) for item in periods) == (
        ("week", "2025-W50"),
        ("week", "2025-W52"),
        ("month", "2025-12"),
        ("year", "2025"),
    )


@pytest.mark.parametrize("value", [True, 2026, "2026-W00", "2026-W54"])
def test_invalid_affected_week_fails_closed(value: object) -> None:
    affected = AffectedPeriods()
    affected.weeks.add(value)  # type: ignore[arg-type]
    with pytest.raises(PeriodSelectionError):
        select_periods(datetime(2026, 8, 4, tzinfo=timezone.utc), (), affected)
```

- [ ] **Step 2: Run tests and prove RED**

Run: `python3 -m pytest -q tests/test_period_archives.py`

Expected: collection fails because `scripts.rki_pipeline.aggregation` does not exist.

- [ ] **Step 3: Implement immutable period parsing and selection**

```python
class AggregationError(ValueError):
    """Base aggregation contract failure."""


class PeriodSelectionError(AggregationError):
    """A due or affected period is malformed, future, or not closed."""


@dataclass(frozen=True, slots=True)
class PeriodRef:
    kind: TaskKind
    value: str
    start: date
    end: date
    source_date_epoch: int


def period_ref(kind: TaskKind, value: str) -> PeriodRef:
    """Parse one exact week/month/year and derive its Berlin close instant."""


def select_periods(
    as_of: datetime,
    due_tasks: Iterable[DueTask],
    affected_periods: AffectedPeriods,
) -> tuple[PeriodRef, ...]:
    """Return closed due/affected periods in stable chronological kind order."""
```

Use `date.fromisocalendar`, calendar-month arithmetic, `ZoneInfo("Europe/Berlin")`, and exact type checks. Reject naive `as_of`, `TaskKind.RECONCILIATION`, future/nonclosed periods, invalid set member types, and boolean years.

- [ ] **Step 4: Add strict period-manifest schema and fixture**

Register this exact top-level contract with `additionalProperties: false`:

```json
{
  "schema_version": "1.0.0",
  "kind": "month",
  "period": "2026-07",
  "timezone": "Europe/Berlin",
  "start_date": "2026-07-01",
  "end_date": "2026-07-31",
  "source_date_epoch": 1785535200,
  "input_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "documents": [],
  "archives": [],
  "month_manifests": []
}
```

Document entries require document/version/source/publication identity plus nullable PDF/Markdown artifact IDs and SHA-256 values. Archive entries require archive ID, kind, relative bundle path, input fingerprint, output SHA-256, bytes, and nullable storage-reference ID. Month-manifest values are canonical repository-relative paths. Arrays are sorted and unique at runtime; schema enforces shapes and bounds.

Update registry count and validator output from 12 to 13. `validate_schemas.py` must load `tests/fixtures/schemas/period-archive-manifest.json` and call `validate_document("period-archive-manifest", fixture)`. Update `test_all_registered_schemas_are_strict_draft_2020_12` accordingly and add valid/unknown-field checks for the fixture.

- [ ] **Step 5: Run focused schema and period tests**

Run:

```bash
python3 -m pytest -q tests/test_period_archives.py tests/test_schemas.py
python3 scripts/validate_schemas.py
python3 scripts/validate_all_baseline.py
```

Expected: all pass; schema validator reports 13 contracts.

- [ ] **Step 6: Commit Task 1**

```bash
git add scripts/rki_pipeline/aggregation.py tests/test_period_archives.py schemas/period-archive-manifest.schema.json tests/fixtures/schemas/period-archive-manifest.json config/schema-registry.json scripts/validate_schemas.py tests/test_schemas.py
git commit -m "feat(p07): define period archive contracts"
```

---

### Task 2: Manifest selection and archive planning

**Files:**
- Modify: `scripts/rki_pipeline/aggregation.py`
- Modify: `tests/test_period_archives.py`

**Interfaces:**
- Consumes: `PeriodRef`, validated `ManifestGraph`, `Mapping[str, PreparedObject]`, P07.1 `ArchiveEntry` and `ArchiveSpec`.
- Produces: `PeriodDocument`, `PlannedArchive`, `PeriodPlan`, `AggregationPlan`, and `plan_period_archives(...)`.

- [ ] **Step 1: Write failing exact-selection tests**

Create fixture helpers from the existing P06 manifest fixtures and real temporary `PreparedObject` values. Test these assertions:

```python
def test_plan_selects_current_documents_and_separates_formats(tmp_path: Path) -> None:
    plan = plan_period_archives(
        as_of=datetime(2026, 8, 4, tzinfo=timezone.utc),
        due_tasks=(due(TaskKind.MONTH, "2026-07"),),
        affected_periods=AffectedPeriods(),
        graph=graph_with_current_and_superseded_documents(tmp_path),
        prepared_by_logical_key=prepared_objects(tmp_path),
    )
    period = plan.periods[0]
    assert tuple(document.document_id for document in period.documents) == (
        "rki-176904-900000001-v2",
    )
    assert tuple(archive.spec.kind for archive in period.archives) == (
        "month-markdown",
        "month-pdf",
    )
    assert all(not entry.path.endswith(".zip") for archive in period.archives for entry in archive.spec.entries)


def test_empty_format_is_omitted_and_mixed_visibility_fails(tmp_path: Path) -> None:
    pdf_only = plan_for_pdf_only_document(tmp_path)
    assert tuple(archive.spec.kind for archive in pdf_only.periods[0].archives) == ("month-pdf",)
    with pytest.raises(AggregationError, match="Sichtbarkeit"):
        plan_for_mixed_visibility(tmp_path)
```

```python
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-storage", "Storage"),
        ("missing-prepared", "PreparedObject"),
        ("size-drift", "Größe"),
        ("sha-drift", "SHA-256"),
        ("source-drift", "Source"),
        ("document-drift", "Dokument"),
        ("conversion-drift", "Conversion"),
        ("unknown-conversion-state", "Konvertierungsstatus"),
        ("basename-collision", "Kollision"),
    ],
)
def test_manifest_join_drift_fails_closed(tmp_path: Path, mutation: str, message: str) -> None:
    kwargs = mutated_plan_inputs(tmp_path, mutation)
    with pytest.raises(AggregationError, match=message):
        plan_period_archives(**kwargs)


def test_year_archive_contains_payloads_not_month_zips(tmp_path: Path) -> None:
    year = plan_for_year(tmp_path).periods[0]
    assert year.period.value == "2025"
    assert year.archives
    assert all(
        entry.path.endswith((".pdf", ".md")) and not entry.path.endswith(".zip")
        for archive in year.archives
        for entry in archive.spec.entries
    )
```

`mutated_plan_inputs` starts from one valid graph/object fixture and applies exactly the named single-field mutation. The current/superseded fixture must contain both versions and assert only the version with `superseded_by is None` survives.

- [ ] **Step 2: Run selection tests and prove RED**

Run: `python3 -m pytest -q tests/test_period_archives.py -k 'selects or omitted or visibility or join or yearly'`

Expected: fails because planning interfaces are absent.

- [ ] **Step 3: Implement exact typed planning records**

```python
@dataclass(frozen=True, slots=True)
class PeriodDocument:
    document_id: str
    version: int
    source_id: str
    publication_date: str
    title: str
    handle: str
    doi: str | None
    conversion_state: str
    pdf: PreparedObject | None
    markdown: PreparedObject | None


@dataclass(frozen=True, slots=True)
class PlannedArchive:
    relative_bundle: str
    spec: ArchiveSpec


@dataclass(frozen=True, slots=True)
class PeriodPlan:
    period: PeriodRef
    documents: tuple[PeriodDocument, ...]
    archives: tuple[PlannedArchive, ...]
    index_path: str | None
    manifest_path: str


@dataclass(frozen=True, slots=True)
class AggregationPlan:
    periods: tuple[PeriodPlan, ...]
    input_fingerprint: str
```

- [ ] **Step 4: Implement graph joins and archive names**

`plan_period_archives` must:

1. call `select_periods`;
2. index graph collections by exact primary keys;
3. select only current documents with `superseded_by is None` and matching `canonical_periods`;
4. resolve PDF/Markdown storage records by canonical relative path;
5. require matching `PreparedObject.logical_key`, checksum, bytes, source/document/conversion identity, rights state, and visibility;
6. build zero, one, or two `ArchiveSpec` values per period using sorted canonical basenames;
7. use archive IDs `rki-<kind>-<period-lower>-<format>`;
8. use weekly names and path under the Monday start month, month paths under `Monate/YYYY/MM`, and year paths under `Jahre/YYYY`;
9. fingerprint the entire ordered plan with `stable_json_dumps`.

Fail if one format would mix visibility. Do not split into undocumented extra archives.

- [ ] **Step 5: Run focused and P07.1 regression tests**

Run:

```bash
python3 -m pytest -q tests/test_period_archives.py -k 'selects or omitted or visibility or join or yearly'
python3 -m pytest -q tests/test_archives.py
ruff check scripts/rki_pipeline/aggregation.py tests/test_period_archives.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add scripts/rki_pipeline/aggregation.py tests/test_period_archives.py
git commit -m "feat(p07): plan period archive products"
```

---

### Task 3: Monthly index and period-manifest rendering

**Files:**
- Modify: `scripts/rki_pipeline/aggregation.py`
- Modify: `tests/test_period_archives.py`

**Interfaces:**
- Consumes: monthly `PeriodPlan`, complete `AggregationPlan`, mapping from archive ID to P07.1 `ArchiveBuild`.
- Produces: `PeriodManifestError`, `render_month_index(period_plan, aggregation_plan) -> bytes`, `render_period_manifest(period_plan, builds) -> bytes`, `validate_period_manifest(payload) -> dict[str, object]`.

- [ ] **Step 1: Write failing canonical rendering tests**

```python
def test_month_index_is_canonical_complete_and_escaped(tmp_path: Path) -> None:
    aggregation_plan = plan_with_title_and_doi(tmp_path, title="A | B <script>")
    period = aggregation_plan.periods[0]
    rendered = render_month_index(period, aggregation_plan)
    assert rendered.endswith(b"\n")
    assert b"A &#124; B &lt;script&gt;" in rendered
    assert b"176904/900000001" in rendered
    assert b"10.1000/example" in rendered
    assert b"converted" in rendered
    assert b"RKI-Einzelartikel-2026-07-06_bis_2026-07-12-PDF" in rendered


def test_period_manifest_is_canonical_and_backend_neutral(tmp_path: Path) -> None:
    period, builds = built_period_fixture(tmp_path)
    payload = render_period_manifest(period, builds)
    value = validate_period_manifest(payload)
    assert payload == stable_json_dumps(value).encode("utf-8")
    assert "storage_backend" not in payload.decode("utf-8")
    assert value["month_manifests"] == [
        "rki/Bulletins/Manifeste/Archive/month/2026-01.json",
        "rki/Bulletins/Manifeste/Archive/month/2026-02.json",
    ]
```

```python
def test_period_manifest_rejects_archive_build_drift(tmp_path: Path) -> None:
    period, builds = built_period_fixture(tmp_path)
    duplicate = {**builds, "duplicate": next(iter(builds.values()))}
    with pytest.raises(PeriodManifestError, match="Archiv-ID"):
        render_period_manifest(period, duplicate)
    for field in ("input_fingerprint", "output_sha256", "size"):
        with pytest.raises(PeriodManifestError, match=field):
            render_period_manifest(period, drifted_builds(builds, field))


def test_month_index_links_cross_boundary_week_and_allows_missing_optional_values(tmp_path: Path) -> None:
    aggregation_plan = plan_for_month_boundary(tmp_path, doi=None, markdown=None)
    period = aggregation_plan.periods[0]
    payload = render_month_index(period, aggregation_plan)
    assert b"2026-07-27_bis_2026-08-02-PDF" in payload
    assert b"| \xe2\x80\x94 |" in payload
    assert b"not_materialized" in payload


def test_manifest_validation_rejects_unknown_field(tmp_path: Path) -> None:
    period, builds = built_period_fixture(tmp_path)
    value = json.loads(render_period_manifest(period, builds))
    value["unknown"] = True
    with pytest.raises(PeriodManifestError):
        validate_period_manifest(stable_json_dumps(value).encode("utf-8"))
```

Render the same documents/builds in reverse input order and assert identical bytes. The year fixture contains documents in January and February only and must yield exactly those two month-manifest paths.

- [ ] **Step 2: Run rendering tests and prove RED**

Run: `python3 -m pytest -q tests/test_period_archives.py -k 'index or manifest or backend'`

Expected: fails because render/validate functions are absent.

- [ ] **Step 3: Implement safe monthly Markdown rendering**

Use a private table-cell escaper that replaces `&`, `<`, `>`, `|`, CR, and LF with safe text. Build only relative links derived from canonical paths. Sort rows by publication date, document ID, source ID. Include count, required metadata, exact conversion state, checksums, and every overlapping week archive link.

- [ ] **Step 4: Implement canonical period-manifest rendering and validation**

```python
class PeriodManifestError(AggregationError):
    """Period-manifest bytes or archive references violate the contract."""
```

Build the exact schema shape from Task 1. Calculate `input_fingerprint` over the same object with the `input_fingerprint` field omitted and `storage_reference` values normalized to `None`; never include run time. Validate with `validate_document("period-archive-manifest", value)` and require byte equality with `stable_json_dumps(value).encode("utf-8")` on readback.

For year periods, derive sorted month-manifest paths from selected documents' canonical months. For week/month periods, use an empty list.

- [ ] **Step 5: Run rendering, schema, and archive regressions**

Run:

```bash
python3 -m pytest -q tests/test_period_archives.py -k 'index or manifest or backend'
python3 -m pytest -q tests/test_schemas.py tests/test_archives.py
python3 scripts/validate_schemas.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/rki_pipeline/aggregation.py tests/test_period_archives.py
git commit -m "feat(p07): render period indexes and manifests"
```

---

### Task 4: Atomic aggregation materialization and late-arrival isolation

**Files:**
- Modify: `scripts/rki_pipeline/aggregation.py`
- Modify: `tests/test_period_archives.py`

**Interfaces:**
- Consumes: `AggregationPlan`, target product root below `temp_root`, `EffectLedger`, `RightsStorageAuthorizer`.
- Produces: `PeriodArchiveMaterialization` and `materialize_period_archives(...)`.

- [ ] **Step 1: Write failing integration/no-op/rollback tests**

```python
def test_late_arrival_changes_only_its_three_historical_periods(tmp_path: Path) -> None:
    first = materialize_fixture(tmp_path, affected=AffectedPeriods())
    before = tree_hashes(first.root)
    affected = AffectedPeriods(weeks={"2026-W17"}, months={"2026-04"}, years={2026})
    second = materialize_fixture(tmp_path, affected=affected, add_late_document=True)
    changed = {path for path, digest in tree_hashes(second.root).items() if before.get(path) != digest}
    allowed_prefixes = (
        "rki/Bulletins/Monate/2026/04/ZIP/Wochen/",
        "rki/Bulletins/Monate/2026/04/ZIP/",
        "rki/Bulletins/Monate/2026/04/Markdown/index.md",
        "rki/Bulletins/Jahre/2026/ZIP/",
        "rki/Bulletins/Manifeste/Archive/week/2026-W17.json",
        "rki/Bulletins/Manifeste/Archive/month/2026-04.json",
        "rki/Bulletins/Manifeste/Archive/year/2026.json",
    )
    assert changed
    assert all(path.startswith(allowed_prefixes) for path in changed)


def test_materialize_noop_preserves_tree_and_ledger(tmp_path: Path) -> None:
    first, ledger = materialize_fixture_with_ledger(tmp_path)
    mtimes = tree_mtimes(first.root)
    event_count = len(ledger.events)
    second = materialize_same_plan(tmp_path, ledger)
    assert second.changed is False
    assert tree_mtimes(second.root) == mtimes
    assert len(ledger.events) == event_count


def test_failure_rolls_back_complete_previous_product_tree(tmp_path: Path, monkeypatch) -> None:
    first = materialize_fixture(tmp_path)
    before = tree_hashes(first.root)
    monkeypatch.setattr(aggregation, "render_period_manifest", raising_renderer)
    with pytest.raises(PeriodManifestError):
        materialize_updated_fixture(tmp_path)
    assert tree_hashes(first.root) == before


def test_corrupt_tree_is_replaced_but_unsafe_targets_fail(tmp_path: Path) -> None:
    first = materialize_fixture(tmp_path)
    corrupt = first.root / first.manifest_paths[0].relative_to(first.root)
    corrupt.write_bytes(b"corrupt")
    repaired = materialize_same_plan(tmp_path, new_ledger(tmp_path))
    assert repaired.changed is True
    validate_period_manifest(repaired.manifest_paths[0].read_bytes())

    symlink = tmp_path / "symlink-target"
    symlink.symlink_to(first.root, target_is_directory=True)
    with pytest.raises(AggregationError, match="Symlink"):
        materialize_plan_at(symlink, tmp_path)
    with pytest.raises(AggregationError, match="außerhalb"):
        materialize_plan_at(tmp_path.parent / "escape", tmp_path)
```

In the no-op test, replace the rights authority with a stale decision and assert failure occurs before reading existing-output equivalence. After success, assert every new outer-ledger target starts with `second.root.as_posix()` and no directory matching `.stage-*` remains below `tmp_path`.

- [ ] **Step 2: Run integration tests and prove RED**

Run: `python3 -m pytest -q tests/test_period_archives.py -k 'late or noop or rollback or corrupt or rights'`

Expected: fails because materialization interfaces are absent.

- [ ] **Step 3: Implement immutable materialization result**

```python
@dataclass(frozen=True, slots=True)
class MaterializedPeriodArchive:
    archive_id: str
    relative_bundle: str
    build: ArchiveBuild


@dataclass(frozen=True, slots=True)
class PeriodArchiveMaterialization:
    root: Path
    archives: tuple[MaterializedPeriodArchive, ...]
    index_paths: tuple[Path, ...]
    manifest_paths: tuple[Path, ...]
    input_fingerprint: str
    changed: bool
```

- [ ] **Step 4: Implement staged whole-plan publication**

`materialize_period_archives` must require exact types, `RunMode.MATERIALIZE`, matching outer ledger/root, a nonsymlink target below `temp_root`, and fresh rights authorization.

Use existing `staged_directory(target, allowed_root=temp_root, replace_existing=True)`. Inside its stage:

1. create an inner `EffectLedger(RunMode.MATERIALIZE, temp_root=stage)`;
2. call P07.1 `materialize_archive` for every planned bundle under the stage;
3. write monthly indexes and validated period manifests with existing safe file primitives;
4. compare canonical file path/mode/size/SHA tuples to an existing target;
5. if identical, abort staging through one private no-change signal, return `changed=False`, and preserve outer ledger/events;
6. otherwise publish the complete stage, then record final regular-file effects in the outer ledger.

On any failure, restore prior target through `staged_directory`, remove translated tentative events, and rethrow as the narrow aggregation error. Returned paths must point to final target, never the staging directory.

- [ ] **Step 5: Run focused and cross-layer regressions**

Run:

```bash
python3 -m pytest -q tests/test_period_archives.py -k 'late or noop or rollback or corrupt or rights'
python3 -m pytest -q tests/test_archives.py tests/test_manifests.py tests/test_storage_contract.py
ruff check scripts/rki_pipeline/aggregation.py tests/test_period_archives.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add scripts/rki_pipeline/aggregation.py tests/test_period_archives.py
git commit -m "feat(p07): materialize period archives atomically"
```

---

### Task 5: CLI, documentation, CI, and complete gate

**Files:**
- Modify: `scripts/rki_pipeline/aggregation.py`
- Modify: `scripts/rki_pipeline/cli.py:1-24`
- Modify: `tests/test_period_archives.py`
- Create: `docs/Wartung/Periodenarchive.md`
- Modify: `rki/Bulletins/README.md`
- Modify: `.github/workflows/p00-baseline.yml:220-228`
- Modify: `.github/workflows/rki-pipeline.yml:97-104`

**Interfaces:**
- Consumes: offline synthetic fixture only.
- Produces: `python -m scripts.rki_pipeline.cli aggregate --as-of <UTC> --mode plan|materialize`.

- [ ] **Step 1: Write failing CLI contract tests**

```python
def test_aggregate_cli_plan_is_deterministic_and_read_only(monkeypatch, capsys) -> None:
    before = repository_fingerprint(ROOT)
    assert cli.main(["aggregate", "--as-of", "2026-01-01T05:00:00Z", "--mode", "plan"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["aggregate", "--as-of", "2026-01-01T05:00:00Z", "--mode", "plan"]) == 0
    second = capsys.readouterr().out
    assert first == second
    assert repository_fingerprint(ROOT) == before


@pytest.mark.parametrize("mode", ["apply", "PLAN", ""])
def test_aggregate_cli_rejects_unsafe_or_unknown_modes(mode: str, capsys) -> None:
    assert cli.main(["aggregate", "--as-of", "2026-01-01T05:00:00Z", "--mode", mode]) == 2
    assert "aggregate:" in capsys.readouterr().err
```

Add materialize double-run equality, invalid/naive timestamp, unknown argument, and stdout-only/no-output-option tests.

- [ ] **Step 2: Run CLI tests and prove RED**

Run: `python3 -m pytest -q tests/test_period_archives.py -k cli`

Expected: fails because router and `main` are absent.

- [ ] **Step 3: Implement thin aggregate CLI**

Add `aggregate` routing in `scripts/rki_pipeline/cli.py`. `aggregation.main` parses exact `--as-of` and `--mode`, constructs the fixed offline manifest/prepared-object fixture in a `TemporaryDirectory`, and prints canonical plan or materialization evidence with `stable_json_dumps`. It never accepts repository, network, output, backend, token, or apply switches.

- [ ] **Step 4: Document operations and add CI smoke**

`docs/Wartung/Periodenarchive.md` must document:

- Berlin period closure and DST;
- week/month/year paths and names;
- due plus late-arrival union;
- no empty/nested ZIP behavior;
- period-manifest fields and backend-neutral resolution;
- no-op, rollback, recovery, and P05 apply boundary.

Update `rki/Bulletins/README.md` with the generated layout. In both workflows run:

```bash
python -m pytest -q tests/test_period_archives.py
python -m scripts.rki_pipeline.cli aggregate --as-of 2026-01-01T05:00:00Z --mode plan
```

- [ ] **Step 5: Run complete local acceptance gate**

Run:

```bash
python3 -m scripts.rki_pipeline.cli aggregate --as-of 2026-01-01T05:00:00Z --mode plan
python3 -m pytest -q tests/test_period_archives.py
python3 -m pytest -q
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 scripts/validate_p04_storage.py
python3 scripts/validate_p05_dispatcher.py
python3 scripts/validate_rights_register.py
python3 scripts/validate_manifests.py --root tests/fixtures/manifests
python3 scripts/validate_fixture_manifest.py
python3 scripts/validate_schemas.py
python3 scripts/validate_ci_mutation_safety.py
python3 -m scripts.validate_requirements
python3 -m compileall -q scripts tests
ruff check scripts tests
npm test
git diff --check
```

Expected: every command passes; plan CLI is byte-identical across two invocations; worktree remains clean except intended source changes before commit.

- [ ] **Step 6: Commit Task 5**

```bash
git add scripts/rki_pipeline/aggregation.py scripts/rki_pipeline/cli.py tests/test_period_archives.py docs/Wartung/Periodenarchive.md rki/Bulletins/README.md .github/workflows/p00-baseline.yml .github/workflows/rki-pipeline.yml
git commit -m "feat(p07): expose period archive aggregation"
```

## Plan self-review

- Every design requirement maps to one task and an executable test.
- Interfaces use existing exact types and remain consistent across tasks.
- No P07.3 reconciliation, repository apply, backend client, scheduler rewrite, or dependency was added.
- Security and rollback checks cover all new trust boundaries.
- All code-producing steps have a RED/GREEN cycle and a bounded commit.
