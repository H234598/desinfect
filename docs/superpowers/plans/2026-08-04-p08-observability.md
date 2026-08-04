# P08.2 Observability Implementation Plan

> Scope: implement only job summary, expiring diagnostics, and one rolling incident issue. Follow TDD; keep transport behind pure planning.

## Task 1: CI summary contract

**Files:** `tests/test_ci_summary.py`, `scripts/rki_pipeline/ci_summary.py`

1. Write failing tests for valid success/failure manifests, fixed operator fields, deterministic task ordering, missing metrics, Markdown/ANSI/secret/e-mail redaction, invalid schema, and 14/30/90 retention.
2. Run `pytest -q tests/test_ci_summary.py`; verify RED.
3. Implement minimal pure renderer and CLI using `validate_document`, `redact_text`, and `atomic_write_text`.
4. Run focused tests and CLI smoke with a failure fixture.

## Task 2: Rolling issue planner and adapter

**Files:** `tests/test_incident_issue.py`, `scripts/rki_pipeline/incident_issue.py`

1. Write failing tests for threshold validation, create/update/reopen/heal/no-op, duplicate marker rejection, redacted deterministic body, fixed repository/label/title, bounded list pagination, exact mutation routes, and token non-disclosure.
2. Run `pytest -q tests/test_incident_issue.py`; verify RED.
3. Implement immutable plan values, pure planner, bounded stdlib REST adapter, and explicit `plan|apply` CLI.
4. Run focused tests and offline plan smoke.

## Task 3: Preserve failure evidence without changing exit

**Files:** `tests/test_pipeline_cli.py` or nearest existing CLI test, `scripts/rki_pipeline/pipeline_cli.py`

1. Add regression: `TransactionError.run_manifest` is written to requested transaction output and command still returns its original non-zero code.
2. Implement narrow exception-path write; do not catch or downgrade unrelated errors.
3. Run CLI and transaction tests.

## Task 4: Workflow, docs, and operational contracts

**Files:** `.github/workflows/rki-pipeline.yml`, `tests/test_p05_workflows.py`, `scripts/validate_p05_dispatcher.py`, `docs/Wartung/Observability.md`, `runbooks/PIPELINE-FAILED.md`, `.github/README.md`

1. Add failing workflow tests for always-run summary, generated summary artifact, state-derived retention, optional dedicated `issues:write` App token, fixed repository operation, and unchanged blocking semantics.
2. Add summary/retention and optional incident steps with immutable existing actions only.
3. Document operator fields, redaction, retention, enable/disable path, threshold, recovery, and rollback.
4. Run workflow validators and focused tests.

## Task 5: Full verification and review

Run:

```bash
python3 scripts/validate_all_baseline.py
python3 scripts/validate_p02_contracts.py
python3 scripts/validate_p05_dispatcher.py
python3 scripts/validate_ci_mutation_safety.py
python3 -m scripts.rki_pipeline.ci_summary tests/fixtures/status/failure.json
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m pytest -q
npm test
ruff check .
git diff --check
```

Then run scoped review, full branch review, PR checks, valid-feedback fixes, squash merge, post-merge CI, and governance closeout.

