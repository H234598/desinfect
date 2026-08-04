# P08.1 Internal Watchdog Design

**Date:** 2026-08-04
**Scope:** P08.1 only
**Approval basis:** approved P08.1 blueprint, presented option A, and standing user approval for autonomous blueprint execution without routine confirmation

## Goal

Plan one deterministic internal-watchdog bark when the persisted deadline is due. A bark advances only watchdog timestamps. It never advances the last successful pipeline run, last successful repository write, or last `main` commit. A successful intended apply commit can arm or reset the watchdog through one explicit function.

P08.1 does not create commits, update GitHub issues, produce job summaries or diagnostic artifacts, contact GitHub, or implement the external Cloudflare watchdog. P08.2 owns summaries, artifacts, and rolling issues. P09 owns the external worker, Durable Object, workflow reactivation, and dispatch idempotency.

## Considered approaches

### 1. Standalone pure watchdog state machine — selected

Add `scripts/rki_pipeline/watchdog.py` with immutable plan values and pure status projections. Route a read-only `watchdog --mode plan` command through the existing domain CLI.

Benefits:

- keeps bark health separate from pipeline success projection;
- reuses the existing schema-valid `status.json` watchdog object;
- makes deadlines, cooldown, reset eligibility, and replay rejection directly testable;
- adds no dependency and no repository writer.

Cost: P05 orchestration must call the reset projection after its successful intended apply commit; P08.1 exposes that contract but does not redesign the existing transaction writer.

### 2. Add watchdog as a normal `DueTask` — rejected

This shares dispatcher ordering, but a watchdog bark has different status and commit semantics from weekly, monthly, yearly, and reconciliation data tasks. Treating it as ordinary ingest work risks advancing pipeline success clocks.

### 3. Fold watchdog projection into `runtime_status.py` — rejected

This minimizes modules but mixes run completion with liveness escalation. The boundary would make the requirement “Bark darf Pipelinegesundheit nicht fälschen” harder to enforce.

## Existing contracts reused

- `status.json` and `schemas/status.schema.json` already contain `interval_days`, `last_reset_at`, `next_bark_at`, `last_bark_at`, and `reset_by`.
- `scripts.rki_pipeline.schema_registry.validate_document("status", value)` remains the status authority.
- `scripts.rki_pipeline.due_tasks.parse_utc` remains the strict aware-UTC parser.
- `scripts.rki_pipeline.io_utils.stable_json_dumps` remains the canonical CLI serializer.
- `pipeline.last_main_commit_at`, `pipeline.last_successful_run_at`, and `pipeline.last_successful_write_at` remain separate and are never inferred from one another.

No status-schema version bump or new persistence field is needed. Repeated escalation is derived safely: a bark is repeated when `last_bark_at` is at or after `last_reset_at`. Cooldown is represented by the advanced `next_bark_at`.

## Public API

`watchdog.py` exposes:

```python
class WatchdogError(ValueError): ...

@dataclass(frozen=True, slots=True)
class BarkPlan:
    evaluated_at: str
    interval_days: int
    expected_next_bark_at: str
    next_bark_at: str
    causes: tuple[str, ...]
    repeated: bool
    commit_title: str
    commit_body: str

    def to_dict(self) -> dict[str, object]: ...

def plan_watchdog(status: dict[str, object], *, as_of: str) -> BarkPlan | None: ...

def apply_bark(status: dict[str, object], plan: BarkPlan) -> dict[str, object]: ...

def reset_watchdog(
    status: dict[str, object],
    *,
    now: str,
    interval_days: int = 45,
    reset_by: str,
    run_mode: str,
    run_status: str,
    commit_created: bool,
) -> dict[str, object]: ...
```

All functions validate input and returned status documents. They return deep copies and never mutate caller-owned dictionaries.

## Deadline and cause rules

`interval_days` must be a real integer in inclusive range 7–55. Boolean values are rejected. The persisted status value is authoritative while planning.

An unarmed watchdog has both `last_reset_at` and `next_bark_at` null. Planning returns `None`. A partially armed pair is invalid. For an armed watchdog, the schedule anchor is `last_bark_at` when it is non-null and at or after `last_reset_at`; otherwise the anchor is `last_reset_at`. `next_bark_at` must equal that anchor plus `interval_days`; inconsistent persisted state fails closed.

Planning returns `None` while `as_of < next_bark_at`. It returns exactly one `BarkPlan` when `as_of >= next_bark_at`. The plan advances the next deadline to `as_of + interval_days`, not to the old deadline plus repeated intervals; one delayed evaluation therefore creates one bark, never a catch-up burst.

At a due evaluation, each pipeline clock is reported independently in stable order:

1. `last_main_commit_missing` or `last_main_commit_stale`;
2. `last_successful_run_missing` or `last_successful_run_stale`;
3. `last_successful_write_missing` or `last_successful_write_stale`.

A non-null clock is stale when it is older than `as_of - interval_days`. Future timestamps fail closed. If no clock is missing or stale but the persisted watchdog deadline is due, cause is `scheduled_keepalive`.

`repeated` uses the same anchor condition: it is true only when a prior `last_bark_at` exists at or after the latest reset. A bark before the latest reset is historical and does not count as repeated escalation.

## Commit message plan

The plan contains text only; it does not invoke Git.

- Fault cause: `chore(wachhund): 45 Tage ohne erfolgreichen Schreiblauf erkannt`, with the effective interval substituted.
- Keepalive only: `chore(wachhund): neues Betriebsupdate nach 45 Tagen Inaktivität`, with the effective interval substituted.
- Body contains evaluated time, trigger `internal-watchdog`, interval, previous `main` commit time, previous successful run time, previous successful write time, causes, repeated flag, and next deadline.

Null observations render as `unbekannt`. Commit text is bounded, deterministic, contains no exception details, and cannot contain external untrusted text.

## Bark projection and replay safety

`apply_bark` accepts only a current status whose `next_bark_at` exactly equals `plan.expected_next_bark_at`. A changed or already advanced deadline raises `WatchdogError`. This optimistic precondition prevents stale or replayed plans from creating another bark projection.

The projection sets:

- `watchdog.last_bark_at = plan.evaluated_at`;
- `watchdog.next_bark_at = plan.next_bark_at`;
- `updated_at = plan.evaluated_at`.

It preserves `watchdog.last_reset_at`, `watchdog.reset_by`, `watchdog.interval_days`, the complete `pipeline` object, periods, corpus, and runtime. It never changes top-level health from degraded or blocked to operational. P08.2 may later render or publish the plan.

## Reset projection

`reset_watchdog` succeeds only when all of these facts are explicit:

- `run_mode == "apply"`;
- `run_status` is `success` or `recovered`;
- `commit_created is True`;
- `reset_by` is a nonempty printable value of at most 120 characters;
- `now` is canonical UTC;
- `interval_days` is in 7–55.

Otherwise it raises `WatchdogError` and returns no status. A successful reset sets `last_reset_at = now`, `next_bark_at = now + interval_days`, `reset_by`, and `interval_days`. It preserves `last_bark_at` as audit history and preserves every pipeline clock. A successful no-op, plan/materialize run, failed run, or apply run without a commit cannot reset the watchdog.

## CLI

The domain router accepts:

```bash
python -m scripts.rki_pipeline.cli watchdog \
  --as-of 2026-09-04T00:00:00Z \
  --mode plan \
  --status status.json
```

Only `plan` is accepted in P08.1. Output is canonical JSON:

```json
{
  "as_of": "2026-09-04T00:00:00Z",
  "bark_plan": null,
  "due": false,
  "mode": "plan",
  "schema_version": "1.0.0"
}
```

When due, `bark_plan` is `BarkPlan.to_dict()` and `due` is true. CLI never applies the projection, writes a file, runs Git, or contacts a network. Expected input errors return exit code 1 with bounded `watchdog: ...` stderr; argparse usage errors return 2.

## Tests

`tests/test_watchdog.py` covers:

- interval boundaries 7 and 55; rejection of bool, 6, and 56;
- unarmed and partially armed status;
- 44 days without bark and due-at-boundary planning;
- delayed evaluation yielding one plan and no catch-up burst;
- independent missing, stale, fresh, and future clock handling;
- stable cause order and deterministic commit text;
- first and repeated bark classification across a reset;
- bark projection preserving all three pipeline clocks;
- stale-plan and replay rejection;
- reset eligibility for apply/success or apply/recovered with a commit;
- rejection of failed, no-op, plan, materialize, and commit-free resets;
- schema-valid reset and bark projections;
- read-only, deterministic CLI output and error handling;
- domain-router delegation.

Existing status-schema, runtime-status, dispatcher, baseline, and full test suites remain green.

## Documentation and CI

- Add `docs/Wartung/Wachhund.md` with three-clock semantics, arming/reset conditions, deadline/cooldown behavior, commit-message contract, recovery, and persisted interval authority. P08.1 has no repository-variable action until an executable caller wires one.
- Add the focused watchdog test and read-only CLI smoke to the existing baseline workflow.
- Keep `status.json` unchanged because P02 already provisioned the complete P08.1 persistence shape with interval 45 and an intentionally unarmed initial state.

## Self-review

- No placeholders or unresolved choices.
- P08.1 remains separate from P08.2 observability and P09 external recovery.
- Existing status schema is reused without speculative fields or migration.
- Bark and reset projections cannot advance or merge the three pipeline clocks.
- No network, repository write, issue mutation, or dependency is added.
