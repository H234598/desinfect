---
title: P05 – Dispatcher, Transaktion und GitHub-App-Writer – Implementierungsplan
aliases:
  - P05 Implementation Plan
  - Dispatcher Transaction Plan
tags:
  - desinfect
  - p05
  - implementation-plan
  - dispatcher
  - transaction
  - github-app
type: implementation-plan
status: completed
created: 2026-07-31T21:35:00Z
date: 2026-07-31
---

# P05 Dispatcher, Transaction, and GitHub App Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one deterministic daily dispatcher and one transaction-safe GitHub-App writer path that executes all due tasks atomically and creates at most one validated commit.

**Architecture:** Pure Python modules calculate due tasks and immutable dispatch/commit plans. One reusable transaction orchestrator composes existing P02/P04 run-status, RunMode, storage, and write-policy primitives. Three workflows expose one daily schedule, one reusable pipeline, and one manual backfill entry while sharing one non-cancelling concurrency group.

**Tech Stack:** Python 3.12 standard library, pytest 9, jsonschema, Git CLI, GitHub Actions, `actions/checkout`, `actions/setup-python`, `actions/setup-node`, and GitHub’s `actions/create-github-app-token` action pinned to a full commit SHA.

## Global Constraints

- Preserve **ADR-003=A** and **ADR-014=B**.
- Satisfy `MUSS-03`, `MUSS-05`, `MUSS-06`, `V2-05-DISPATCH-001`–`010`, and `V2-14-GIT-001`–`007`.
- Keep exactly one scheduled writer entry point.
- Use one shared concurrency group: `desinfect-repository-writer` with `cancel-in-progress: false`.
- `plan` and `materialize` must not modify repository files, Git index, commits, LFS objects, releases, or object storage.
- A transaction executes all due tasks, one global validation, and at most one commit.
- Automatic writes remain limited by `config/automatic-write-paths.toml`.
- Never force-push.
- Never fall back from the GitHub App to a write-capable `GITHUB_TOKEN`.
- Keep workflows read-only until all deterministic validation gates have passed.
- P05 performs no real RKI network fetch or historical full backfill.
- Every production path must be covered by offline tests.

---

## File map

### Create

- `scripts/rki_pipeline/due_tasks.py` — pure UTC period and catch-up calculation.
- `scripts/rki_pipeline/dispatch_plan.py` — immutable versioned dispatch contract.
- `scripts/rki_pipeline/dispatcher.py` — stdout-only daily/backfill plan CLI.
- `scripts/rki_pipeline/transaction.py` — all-or-nothing task orchestration.
- `scripts/rki_pipeline/commit_plan.py` — deterministic staged-tree and commit contract.
- `scripts/rki_pipeline/git_writer.py` — exact stage/validate/commit/push implementation.
- `scripts/rki_pipeline/pipeline_cli.py` — workflow-facing transaction CLI.
- `scripts/validate_p05_dispatcher.py` — blocking P05 repository/workflow validator.
- `scripts/validate_ci_mutation_safety.py` — updated Variant-B workflow validator from PR #8.
- `config/dispatcher.toml` — strict daily and catch-up limits.
- `.github/workflows/rki-dispatcher.yml` — only scheduled dispatcher.
- `.github/workflows/rki-pipeline.yml` — reusable/manual transactional writer.
- `.github/workflows/rki-backfill.yml` — manual bounded backfill caller.
- `tests/test_due_tasks.py`
- `tests/test_dispatch_plan.py`
- `tests/test_dispatcher.py`
- `tests/test_write_transaction.py`
- `tests/test_commit_plan.py`
- `tests/test_git_writer.py`
- `tests/test_pipeline_cli.py`
- `tests/test_p05_workflows.py`
- `tests/test_ci_mutation_safety.py`

### Modify

- `.github/workflows/p00-baseline.yml` — run P05 and CI-mutation validators.
- `scripts/validate_all_baseline.py` — include P05 structural validator when present.
- `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md` — P05 in review after implementation.
- `docs/IMPLEMENTIERUNGSSTATUS.md` — active P05 evidence.
- `docs/implementation-status.json` — P05 machine status.
- `config/plan-source.json` — synchronized control-file hash.
- `docs/Wartung/Automatische-Schreibpfade.md` — writer contract.
- `docs/Wartung/Status-und-Recovery.md` — dispatcher clocks/watermarks.
- `README.md`, `SECURITY.md`, `PROVENANCE.md` — P05 entry points and boundaries.

---

### Task 1: Pure due-task and catch-up calculation

**Files:**
- Create: `config/dispatcher.toml`
- Create: `scripts/rki_pipeline/due_tasks.py`
- Test: `tests/test_due_tasks.py`

**Interfaces:**
- Consumes: schema-valid `status.json`, explicit RFC3339 UTC timestamp.
- Produces: `DispatchConfig`, `TaskKind`, `DueTask`, `calculate_due_tasks()`, `calculate_backfill_tasks()`.

- [x] **Step 1: Write failing boundary tests**

```python
def test_week_catchup_stops_at_last_closed_iso_week():
    tasks = calculate_due_tasks(status(last_week="2026-W29"), "2026-07-31T12:00:00Z", limits())
    assert [task.period for task in tasks if task.kind is TaskKind.WEEK] == ["2026-W30"]


def test_missing_watermarks_do_not_start_1994_backfill():
    tasks = calculate_due_tasks(status(), "2026-07-31T12:00:00Z", limits())
    assert sum(task.kind is TaskKind.WEEK for task in tasks) <= 1
    assert sum(task.kind is TaskKind.MONTH for task in tasks) <= 1
    assert sum(task.kind is TaskKind.YEAR for task in tasks) <= 1
```

Cover ISO year rollover, leap-year February, future watermarks, 92-day reconciliation, hard limits, deterministic ordering, malformed TOML types, and explicit bounded backfill.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m pytest -q tests/test_due_tasks.py`

Expected: import failure for `scripts.rki_pipeline.due_tasks`.

- [x] **Step 3: Implement strict config and immutable task types**

Implement:

```python
class TaskKind(StrEnum):
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    RECONCILIATION = "reconciliation"

@dataclass(frozen=True, slots=True)
class DueTask:
    task_id: str
    kind: TaskKind
    period: str
    reason: str
    due_at: str
```

Reject booleans as integers, unknown keys, non-UTC timestamps, invalid ISO periods, and zero/negative limits.

- [x] **Step 4: Implement period iteration and catch-up rules**

Use `datetime`, `date`, `timezone`, and `calendar`; do not use local time or third-party date libraries. Generate only completed periods and sort by `(kind_order, period)`.

- [x] **Step 5: Run tests and commit**

Run:

```bash
python3 -m pytest -q tests/test_due_tasks.py
python3 -m compileall -q scripts/rki_pipeline/due_tasks.py tests/test_due_tasks.py
```

Commit: `feat(p05): add deterministic due-task calculation`

---

### Task 2: Immutable dispatch plan and stdout-only dispatcher

**Files:**
- Create: `scripts/rki_pipeline/dispatch_plan.py`
- Create: `scripts/rki_pipeline/dispatcher.py`
- Test: `tests/test_dispatch_plan.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `DueTask`, `RunMode`, `StorageBackend`, status JSON, base SHA.
- Produces: `DispatchPlan`, `DispatchPlan.from_dict()`, `DispatchPlan.to_dict()`, `DispatchPlan.sha256`, `build_daily_plan()`, `build_backfill_plan()`.

- [x] **Step 1: Write failing contract tests**

Test canonical hash stability, duplicate task rejection, sort enforcement, exact keys, 40-character lowercase base SHA, strict UTC timestamps, unknown modes/backends, stdout-only CLI, and empty-plan success.

- [x] **Step 2: Verify RED**

Run: `python3 -m pytest -q tests/test_dispatch_plan.py tests/test_dispatcher.py`

- [x] **Step 3: Implement canonical serialization**

Use `stable_json_dumps()` and SHA-256. `from_dict()` validates exact keys and reconstructs typed tasks without `str()`/`int()` coercion.

- [x] **Step 4: Implement CLI**

Daily CLI arguments:

```text
--config config/dispatcher.toml
--status status.json
--now <UTC-Z>
--base-sha <40-hex>
--trigger schedule|workflow_dispatch
--run-mode plan|materialize|apply
```

Backfill subcommand arguments:

```text
backfill --from-year 1994 --to-year 2020 --max-tasks 1000 --run-mode plan|materialize|apply
```

Output exactly one JSON object to stdout and diagnostics to stderr. No output-file option.

- [x] **Step 5: Run tests and commit**

Commit: `feat(p05): add versioned dispatch plans`

---

### Task 3: Transaction protocol and single-validation orchestration

**Files:**
- Create: `scripts/rki_pipeline/transaction.py`
- Test: `tests/test_write_transaction.py`

**Interfaces:**
- Consumes: `DispatchPlan`, existing `new_run()`, `update_run()`, `RunMode`, `SideEffectGuard`, `WriteOperation`.
- Produces: `TaskPlan`, `TaskResult`, `TaskHandler`, `TransactionContext`, `TransactionResult`, `execute_transaction()`.

- [x] **Step 1: Write failing all-or-nothing tests**

Required cases:

```python
def test_multiple_tasks_share_one_validation_and_one_commit_plan(): ...
def test_second_task_failure_prevents_every_apply(): ...
def test_validation_failure_prevents_apply_and_commit(): ...
def test_all_noop_tasks_return_commit_required_false(): ...
def test_status_watermarks_change_only_after_verify(): ...
def test_dispatch_base_sha_mismatch_blocks_before_plan(): ...
```

- [x] **Step 2: Verify RED**

Run: `python3 -m pytest -q tests/test_write_transaction.py`

- [x] **Step 3: Implement typed handler ports**

Handlers must not receive Git credentials. `plan()` executes under `RunMode.PLAN`, `materialize()` under one shared temp root, and `apply()` only after the global validator succeeds.

- [x] **Step 4: Implement state-machine integration**

Create one run manifest containing all task IDs. Transition through `plan`, `materialize`, `validate`, `apply`, `verify`, and `complete`. On failure, store a redacted structured error and return no commit plan.

- [x] **Step 5: Prove one validation call**

The validator is an injected callable returning `None` or raising. Call it exactly once after all materialization and before any apply.

- [x] **Step 6: Run tests and commit**

Commit: `feat(p05): orchestrate tasks in one transaction`

---

### Task 4: Deterministic commit plan

**Files:**
- Create: `scripts/rki_pipeline/commit_plan.py`
- Test: `tests/test_commit_plan.py`

**Interfaces:**
- Consumes: validated changed paths, blob hashes, Git modes, base SHA, dispatch SHA.
- Produces: `TreeEntry`, `CommitPlan`, `build_commit_plan()`.

- [x] **Step 1: Write failing tree-hash tests**

Verify path-order independence, mode sensitivity, content sensitivity, duplicate/collision rejection, no external title data, deterministic subject/body, and empty-change rejection.

- [x] **Step 2: Verify RED**

- [x] **Step 3: Implement exact typed validation**

`TreeEntry.path` uses `normalize_posix_path`; mode is one of `100644`, `100755`; blob SHA is lowercase SHA-256. `CommitPlan.expected_base_sha` is lowercase 40-hex.

- [x] **Step 4: Implement tree SHA and message**

Hash newline-delimited canonical rows: `<mode>\0<path>\0<sha256>\n`. Subject: `chore(rki): apply <N> scheduled task(s)`. Body lists sorted task IDs and `Dispatch-Plan-SHA256:`.

- [x] **Step 5: Run tests and commit**

Commit: `feat(p05): add deterministic commit contract`

---

### Task 5: Exact local Git writer and Variant-B safety validator

**Files:**
- Create: `scripts/rki_pipeline/git_writer.py`
- Create: `scripts/validate_ci_mutation_safety.py`
- Test: `tests/test_git_writer.py`
- Test: `tests/test_ci_mutation_safety.py`
- Modify: `docs/Wartung/Automatische-Schreibpfade.md`

**Interfaces:**
- Consumes: `CommitPlan`, `validate_index()`, injected `GitRunner`.
- Produces: `GitWriteResult`, `apply_commit_plan()`, repository-level CI mutation findings.

- [x] **Step 1: Port PR #8 tests and add P05 writer tests**

Cover CIW001–CIW011, multiple writers in one step/workflow, multi-line audit bypasses, no-op staged diff, extra staged path, wrong base SHA, changed remote base, exact one commit, no force push, and token-free command diagnostics.

- [x] **Step 2: Verify RED**

- [x] **Step 3: Implement GitRunner and repository snapshots**

All subprocess calls use argument arrays, `check=True`, captured output, no shell. Reject non-local repository roots and symlinked `.git` paths.

- [x] **Step 4: Implement write sequence**

```text
assert clean baseline
assert HEAD == expected_base_sha
stage exact changed_paths
validate_index
print status/diff diagnostics
if staged diff empty -> no_op
verify staged tree hash
commit once
fetch origin main
assert origin/main == expected_base_sha
push HEAD:main without force
```

The token is supplied through Git’s HTTP extra-header environment by the workflow and is never part of arguments, remotes, exceptions, or logs.

- [x] **Step 5: Integrate Variant-B validator**

Adapt PR #8 to current workflows. Require every `git commit`/`git push` step to include no-op guard and diagnostics; reject audit bypasses and mutations outside analyzable steps.

- [x] **Step 6: Run tests and commit**

Commit: `feat(p05): add exact Git writer safety gates`

---

### Task 6: Workflow-facing pipeline CLI

**Files:**
- Create: `scripts/rki_pipeline/pipeline_cli.py`
- Test: `tests/test_pipeline_cli.py`

**Interfaces:**
- Consumes: DispatchPlan JSON from stdin/path, transaction handlers, Git writer.
- Produces: commands `execute`, `validate-plan`, `commit` with machine-readable outputs.

- [x] **Step 1: Write failing CLI tests**

Cover malformed plans, base mismatch, no-op, missing confirmation for apply, missing app token at commit time, secret redaction, and exit codes `0=success/no-op`, `2=blocked/config`, `3=transaction failed`.

- [x] **Step 2: Verify RED**

- [x] **Step 3: Implement placeholder-safe P05 handlers**

Until P06/P07 exist, register deterministic infrastructure handlers that can update only run/status metadata and produce no archive content. Unknown task kinds fail closed; no fake completion watermark is written for unimplemented domain work.

- [x] **Step 4: Implement commands and JSON outputs**

The CLI returns `run-manifest`, transaction result, changed paths, and commit-required flag. It never emits credentials.

- [x] **Step 5: Run tests and commit**

Commit: `feat(p05): expose transactional pipeline CLI`

---

### Task 7: Dispatcher, pipeline, and backfill workflows

**Files:**
- Create: `.github/workflows/rki-dispatcher.yml`
- Create: `.github/workflows/rki-pipeline.yml`
- Create: `.github/workflows/rki-backfill.yml`
- Test: `tests/test_p05_workflows.py`

**Interfaces:**
- Dispatcher emits compact base64 or single-line JSON plan and `has_tasks` output.
- Pipeline accepts `dispatch_plan_json` and `confirm_apply` inputs.
- Backfill calls pipeline with bounded manual inputs.

- [x] **Step 1: Write workflow contract tests**

Parse YAML using `yaml.safe_load`. Assert:

- one and only one repository `schedule`;
- dispatcher schedule plus manual trigger;
- pipeline `workflow_call` and manual trigger;
- backfill manual-only;
- all writer paths use `desinfect-repository-writer` and `cancel-in-progress: false`;
- `permissions: contents: read` at workflow level;
- GitHub App token action occurs after validation;
- no write-token fallback;
- checkout uses `persist-credentials: false`;
- pipeline never force-pushes;
- backfill apply requires literal `APPLY` confirmation.

- [x] **Step 2: Verify RED**

- [x] **Step 3: Implement dispatcher workflow**

Use one daily cron at `17 04 * * *` UTC and `workflow_dispatch`. Calculate `main` SHA from checkout and generate plan to `$GITHUB_OUTPUT` without writing the repository.

- [x] **Step 4: Implement reusable pipeline workflow**

Setup dependencies, validate plan, execute plan/materialize/validate/apply, create a repository-scoped GitHub App token using `WACHHUND_APP_CLIENT_ID` and `WACHHUND_APP_PRIVATE_KEY`, then run exact writer. Keep token creation and Git push in one job because installation tokens are revoked in the action post-step and expire after one hour.

- [x] **Step 5: Implement manual backfill workflow**

Validate year bounds and maximum tasks before constructing the plan. Call the same reusable pipeline; do not duplicate writer steps.

- [x] **Step 6: Run tests and commit**

Commit: `ci(p05): add dispatcher pipeline and backfill workflows`

---

### Task 8: Blocking P05 validator and baseline integration

**Files:**
- Create: `scripts/validate_p05_dispatcher.py`
- Modify: `.github/workflows/p00-baseline.yml`
- Modify: `scripts/validate_all_baseline.py`
- Test: `tests/test_p05_workflows.py`

**Interfaces:**
- Produces one blocking command: `python3 scripts/validate_p05_dispatcher.py`.

- [x] **Step 1: Write failing validator tests**

Mutate fixture workflows to prove the validator rejects a second schedule, divergent concurrency, write-capable default permissions, token-before-validation, force push, missing Variant-B diagnostics, and absent P05 test files.

- [x] **Step 2: Verify RED**

- [x] **Step 3: Implement structural and behavioral validator**

The validator loads dispatcher config, exercises due-task boundaries, verifies workflow contracts, runs Variant-B analysis, validates exact requirement IDs, and prints a stable success line.

- [x] **Step 4: Add CI steps**

Add P05 validator and focused tests before full unittest/pytest. Expand path filters to include all P05 files and workflows.

- [x] **Step 5: Run tests and commit**

Commit: `ci(p05): block unsafe dispatcher and writer changes`

---

### Task 9: Documentation, provenance, and active plan status

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `PROVENANCE.md`
- Modify: `docs/Wartung/Automatische-Schreibpfade.md`
- Modify: `docs/Wartung/Status-und-Recovery.md`
- Modify: `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`
- Modify: `docs/IMPLEMENTIERUNGSSTATUS.md`
- Modify: `docs/implementation-status.json`
- Modify: `config/plan-source.json`

- [x] **Step 1: Document operator prerequisites**

Document GitHub App `Wachhund`, repository variable `WACHHUND_APP_CLIENT_ID`, secret `WACHHUND_APP_PRIVATE_KEY`, required repository permission `Contents: Read and write`, installation scope limited to `H234598/desinfect`, and absence of a `GITHUB_TOKEN` write fallback.

- [x] **Step 2: Document daily/backfill behavior**

Explain UTC cron, catch-up limits, one transaction/validation/commit, no-op behavior, concurrency, retry on base drift, and manual `APPLY` confirmation.

- [x] **Step 3: Record PR #8 provenance**

State that Variant-B concepts were adapted from `agent/ci-variant-b-20260730@8abf3b0071046ecd3ce3bc4547c63b69a5286fac`; preserve authorship and note P05 integration changes.

- [x] **Step 4: Mark P05 packages `im_review`**

Set active branch/PR after opening the implementation PR. Keep checkboxes open until merge and separate closeout evidence.

- [x] **Step 5: Recompute control hash and commit**

Commit: `docs(p05): document transactional dispatcher operations`

---

### Task 10: Full verification, PR, review repair, merge, and evidence closeout

**Files:**
- All P05 files.
- Separate closeout branch after implementation merge.

- [x] **Step 1: Run complete local-equivalent verification**

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p01_foundation.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p03_grabber.py
python3 scripts/validate_p04_storage.py
python3 scripts/validate_p05_dispatcher.py
python3 scripts/validate_ci_mutation_safety.py
python3 -m scripts.rki_pipeline.dispatcher --help
python3 -m scripts.rki_pipeline.pipeline_cli --help
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
```

- [x] **Step 2: Open implementation PR**

Include explicit non-goals, GitHub App prerequisites, test counts, no real RKI/backfill/write run, and locked ADRs.

- [x] **Step 3: Repair until all gates are green**

Address every still-valid CodeRabbit finding with a regression test. Require GitHub Actions, CodeRabbit, qlty, and zero unresolved threads.

- [x] **Step 4: Squash merge with expected head SHA**

Never merge a moved or unverified head.

- [x] **Step 5: Create separate closeout PR**

Set P05.1–P05.4 to `umgesetzt`, close checkboxes, attach merge SHA, CI run, check statuses, tests, requirement IDs, acceptance timestamp, and next phase P06.

- [x] **Step 6: Close PR #8 as superseded**

Only after the P05 implementation containing its safety contract is merged. Link the replacement PR and preserve provenance.

## Closeout Evidence

- Implementation PR: #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`, verified head `d9e1c5b39cc7fb714ba61d089119bfa5b81c080b`.
- Gates: GitHub Actions `30784217751`, CodeRabbit and qlty successful; 0 unresolved and 10 resolved review threads.
- Tests: 249 Pytest, 9 Unittest, and 2 Node tests successful.
- Requirements: `MUSS-03`, `MUSS-05`, `MUSS-06`, `V2-05-DISPATCH-001` through `V2-05-DISPATCH-010`, and `V2-14-GIT-001` through `V2-14-GIT-007`.
- Accepted at `2026-08-03T04:29:27Z` by `H234598`; next phase: P06.
- Superseded PR #8 was closed unmerged at `2026-08-03T04:32:36Z` after replacement comment `5162329352` linked PR #12 and its merge.

## Plan self-review

- Coverage: all four P05 packages, all P05 MUSS requirements, both P05 V2 prefixes, workflows, writer safety, tests, documentation, and closeout are mapped to tasks.
- Placeholder scan: no `TBD`, deferred implementation, or undefined interface remains.
- Type consistency: `DueTask`, `DispatchPlan`, `TransactionResult`, `CommitPlan`, `EffectLedger`, and `WriteOperation` names are stable across tasks.
- Scope: P05 provides scheduling/transaction/writer infrastructure only; P06/P07 domain generation remains excluded.
