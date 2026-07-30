# P04 RunModes and Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement strict `plan|materialize|apply` isolation, a real `lfs|release|object` storage abstraction, Git-LFS integrity/budget gates, and an idempotent backend migration tool.

**Architecture:** A mode-aware effect ledger and snapshot guard sits in front of focused storage adapters. Every adapter consumes immutable intents, materializes only below an explicit temporary root, and publishes only in `apply`; remote behavior is injected behind small client protocols. Git LFS remains the initial backend and is verified by exact tracking rules, pointer/object integrity, and budgets.

**Tech Stack:** Python 3.12 standard library, `typing.Protocol`, TOML via `tomllib`, JSON Schema Draft 2020-12, Git CLI for read-only snapshots, pytest with offline fakes.

## Global Constraints

- **ADR-003=A** and **ADR-014=B** remain unchanged.
- Unknown RunModes, backends, config keys, paths and object formats fail closed.
- `plan` performs no file or remote writes.
- `materialize` writes only below its explicit `temp_root` and never changes repository files, Git index/HEAD, `status.json`, LFS, releases or object storage.
- `apply` may persist only explicitly registered, deny-first validated effects.
- No real GitHub, LFS transfer, release or object-storage network call occurs in P04 tests.
- Source objects are never automatically deleted during migration.
- All durable references validate against `schemas/storage-reference.schema.json`.

---

### Task 1: RunMode and SideEffectGuard

**Files:**
- Create: `scripts/rki_pipeline/run_modes.py`
- Create: `tests/test_run_modes.py`

**Interfaces:**
- Produces: `RunMode`, `EffectKind`, `EffectEvent`, `EffectLedger`, `RepositorySnapshot`, `SideEffectGuard`, `ModeViolation`.
- `SideEffectGuard(repository_root: Path, mode: RunMode, temp_root: Path | None, ledger: EffectLedger, protected_paths: tuple[str, ...] = ("status.json",))`.

- [ ] **Step 1: Write failing mode-matrix tests**

```python
@pytest.mark.parametrize("mode,effect,allowed", [
    (RunMode.PLAN, EffectKind.REPOSITORY_FILE, False),
    (RunMode.MATERIALIZE, EffectKind.TEMP_FILE, True),
    (RunMode.MATERIALIZE, EffectKind.GIT_INDEX, False),
    (RunMode.APPLY, EffectKind.REPOSITORY_FILE, True),
])
def test_mode_matrix(mode, effect, allowed):
    ledger = EffectLedger(mode)
    if allowed:
        ledger.record(effect, "x")
    else:
        with pytest.raises(ModeViolation):
            ledger.record(effect, "x")
```

- [ ] **Step 2: Verify the tests fail**

Run: `python3 -m pytest -q tests/test_run_modes.py`
Expected: import failure because `run_modes.py` does not exist.

- [ ] **Step 3: Implement exact enums, ledger and snapshot guard**

Implement strict `StrEnum` members, immutable `EffectEvent`, a ledger that checks the mode matrix before append, and Git snapshots from `git rev-parse HEAD`, `git status --porcelain=v1 -z`, and `git diff --cached --binary`. Hash protected files with SHA-256. On context exit compare snapshots and reject unregistered changes; for `materialize`, additionally verify every filesystem event is below `temp_root`.

- [ ] **Step 4: Add negative snapshot tests**

Cover repository file mutation, staged index mutation, `status.json` mutation, a materialize path outside `temp_root`, and a valid temp-only materialization.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest -q tests/test_run_modes.py`
Expected: PASS.

Commit: `feat(p04): RunModes und Seiteneffektwächter einführen`

---

### Task 2: Storage contracts and strict configuration

**Files:**
- Create: `config/storage.toml`
- Create: `scripts/rki_pipeline/storage/__init__.py`
- Create: `scripts/rki_pipeline/storage/base.py`
- Create: `scripts/rki_pipeline/storage/config.py`
- Create: `scripts/rki_pipeline/storage/factory.py`
- Create: `tests/test_storage_contract.py`

**Interfaces:**
- Produces: `StorageBackend`, `StorageIntent`, `PreparedObject`, `StorageReference`, `StorageAdapter`, `StorageConfig`, `load_storage_config`, `build_storage_adapter`.
- `StorageAdapter.materialize(intent, *, temp_root) -> PreparedObject`
- `StorageAdapter.apply(prepared, *, ledger) -> StorageReference`
- `StorageAdapter.verify(reference) -> None`
- `StorageAdapter.list_references() -> tuple[StorageReference, ...]`

- [ ] **Step 1: Write failing contract/config tests**

Test exact backend values, unknown backend rejection, unknown TOML keys, strict integer/boolean/string types, immutable namespaces, SHA-256/size verification and JSON-schema-valid reference serialization.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_storage_contract.py`
Expected: import failure.

- [ ] **Step 3: Implement immutable types and strict TOML loader**

`StorageIntent.from_path()` streams SHA-256 and size but performs no writes. `PreparedObject` requires a path beneath `temp_root`. `StorageReference.to_dict()` uses exactly the schema fields. `load_storage_config()` rejects unknown tables/keys and loads default backend `lfs`.

- [ ] **Step 4: Implement factory fail-closed behavior**

Factory accepts injected `release_client` and `object_client`. Building remote adapters without their required client raises `StorageConfigurationError`.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest -q tests/test_storage_contract.py`
Expected: PASS.

Commit: `feat(p04): echtes Storage Protocol und strikte Konfiguration`

---

### Task 3: Git-LFS adapter, pointer integrity and budgets

**Files:**
- Create: `.gitattributes`
- Create: `scripts/rki_pipeline/storage/lfs.py`
- Create: `tests/test_storage_lfs.py`
- Modify: `config/storage.toml`

**Interfaces:**
- Produces: `LfsPointer`, `LfsBudget`, `LfsInventory`, `LfsStorageAdapter`, `parse_lfs_pointer`, `validate_lfs_tracking`, `verify_lfs_object`, `check_lfs_budget`.

- [ ] **Step 1: Write failing LFS tests**

Cover exact tracking rules for `rki/Bulletins/**/*.pdf`, `rki/Bulletins/Quellen/**/*.md`, and `rki/Bulletins/**/*.zip`; malformed pointer, wrong OID, wrong size, missing object, wrong object hash, per-run object/byte overflow, warning threshold and hard total threshold.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_storage_lfs.py`
Expected: import/file failure.

- [ ] **Step 3: Implement pointer/object verification**

Parse exactly:

```text
version https://git-lfs.github.com/spec/v1
oid sha256:<64 lowercase hex>
size <non-negative integer>
```

Resolve local object as `.git/lfs/objects/<oid[:2]>/<oid[2:4]>/<oid>` and stream-verify size and SHA-256.

- [ ] **Step 4: Implement budget and adapter**

The adapter materializes with the P01 atomic IO primitives into `temp_root`; `apply` writes only canonical repository paths, records `LFS` and `REPOSITORY_FILE` effects, validates tracking before write, and verifies the resulting reference. It never commits or pushes.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest -q tests/test_storage_lfs.py tests/test_run_modes.py`
Expected: PASS.

Commit: `feat(p04): Git-LFS-Integrität und Budgetgates implementieren`

---

### Task 4: Release and object adapters with offline ports

**Files:**
- Create: `scripts/rki_pipeline/storage/release.py`
- Create: `scripts/rki_pipeline/storage/object.py`
- Create: `tests/test_storage_remote.py`

**Interfaces:**
- Produces: `ReleaseClient(Protocol)`, `ObjectClient(Protocol)`, `ReleaseStorageAdapter`, `ObjectStorageAdapter`, `MemoryReleaseClient`, `MemoryObjectClient` test fakes.

- [ ] **Step 1: Write failing remote-adapter tests**

Assert `plan` has zero client calls, `materialize` has zero client calls and writes only below `temp_root`, `apply` makes exactly one idempotent publish call, checksum conflicts fail closed, and returned references are backend-neutral.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_storage_remote.py`
Expected: import failure.

- [ ] **Step 3: Implement injected clients and adapters**

Release client methods: `head(asset_key)`, `put(asset_key, source_path, sha256, size)`, `list(prefix)`. Object client uses the same semantic contract with bucket/namespace. No adapter imports a GitHub/cloud SDK.

- [ ] **Step 4: Run and commit**

Run: `python3 -m pytest -q tests/test_storage_remote.py tests/test_storage_contract.py tests/test_run_modes.py`
Expected: PASS.

Commit: `feat(p04): Release- und Object-Adapter hinter Ports ergänzen`

---

### Task 5: Deterministic backend migration

**Files:**
- Create: `scripts/rki_pipeline/storage/migrate.py`
- Create: `scripts/rki_pipeline/storage_cli.py`
- Create: `tests/test_storage_migration.py`

**Interfaces:**
- Produces: `MigrationState`, `MigrationEntry`, `MigrationPlan`, `plan_migration`, `materialize_migration`, `apply_migration`, CLI subcommands `plan`, `materialize`, `apply`, `verify`.

- [ ] **Step 1: Write failing migration tests**

Cover deterministic ordering, identical target = `unchanged`, missing target = `copy`, conflicting target = `conflict`, source never deleted, materialize only below temp root, apply idempotency and resume after a partially completed fake-client run.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_storage_migration.py`
Expected: import failure.

- [ ] **Step 3: Implement immutable migration plan**

Plan entries are sorted by `artifact_id`; plan SHA-256 is calculated from stable JSON. `materialize_migration()` requires `RunMode.MATERIALIZE`; `apply_migration()` requires `RunMode.APPLY`, rejects unresolved conflicts and verifies every target after publish.

- [ ] **Step 4: Implement CLI and no-op behavior**

CLI prints JSON to stdout in `plan`, writes only below `--temp-root` in `materialize`, and requires explicit `--apply` for publication. Unknown backend exits nonzero.

- [ ] **Step 5: Run and commit**

Run: `python3 -m pytest -q tests/test_storage_migration.py tests/test_storage_remote.py tests/test_storage_lfs.py`
Expected: PASS.

Commit: `feat(p04): idempotente Backendmigration ergänzen`

---

### Task 6: Validators, CI, documentation and plan status

**Files:**
- Create: `scripts/validate_p04_storage.py`
- Create: `docs/Wartung/RunModes-und-Storage.md`
- Modify: `.github/workflows/p00-baseline.yml`
- Modify: `.github/CODEOWNERS`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `PROVENANCE.md`
- Modify: `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`
- Modify: `docs/IMPLEMENTIERUNGSSTATUS.md`
- Modify: `docs/implementation-status.json`
- Modify: `config/plan-source.json`

**Interfaces:**
- Produces: one blockierendes `python3 scripts/validate_p04_storage.py` gate and public P04 documentation.

- [ ] **Step 1: Write validator assertions**

Validate exact `.gitattributes`, strict storage config, importability, schema-valid sample reference, no network SDK imports, migration CLI help, mode matrix and ADR locks.

- [ ] **Step 2: Add read-only CI steps**

The baseline workflow runs the P04 validator, `storage_cli --help`, full unittest/pytest/Node suites. It retains `permissions: contents: read`, no secrets and no repository writes.

- [ ] **Step 3: Update documentation and provenance**

Document mode/effect matrix, storage references, LFS object paths/budgets, remote ports, migration recovery, no source deletion and P04 scope exclusions.

- [ ] **Step 4: Mark P04 `im_review`, not `umgesetzt`**

Set P04.1–P04.4 to `im_review` only after opening the implementation PR. Keep checkboxes open until merge plus closeout evidence.

- [ ] **Step 5: Run complete gate**

Run:

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 scripts/validate_p04_storage.py
python3 -m scripts.rki_pipeline.storage_cli --help
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

Expected: all commands PASS.

- [ ] **Step 6: Open PR and process review to merge**

Open a non-draft P04 PR, repair every actionable CodeRabbit/qlty/CI finding with a regression test, merge only with expected head SHA, then create a separate closeout PR with merge SHA, CI run, review count, test commands and requirements `MUSS-13`, `MUSS-15`, `MUSS-16`, `MUSS-17`, `MUSS-18`, `MUSS-34`, and `V2-12-LFS-001` through `V2-12-LFS-016`.
