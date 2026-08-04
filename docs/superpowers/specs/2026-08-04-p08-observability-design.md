# P08.2 Observability Design

**Date:** 2026-08-04  
**Scope:** P08.2 only — job summary, expiring diagnostics, rolling incident issue

## Goal

Turn one schema-valid run manifest into a safe operator summary and maintain at most one repository-scoped incident issue after repeated failures. Diagnostic handling must never hide or replace the pipeline exit status.

## Boundaries

- Reuse `run-manifest`, `status`, `runtime_status.redact_text`, schema validation, and pinned `upload-artifact`.
- Do not add a status-schema version, service, dependency, Cloudflare behavior, workflow reactivation, or external dispatch.
- Do not accept repository, label, or title from pipeline data. Repository stays `H234598/desinfect`; marker, label, and title prefix are constants.
- GitHub writes require an explicitly enabled workflow path and a short-lived Wachhund App token with `issues:write` only.

## Chosen Shape

### Safe CI summary

`scripts/rki_pipeline/ci_summary.py` validates the run manifest and optional public status before rendering fixed Markdown sections. It reports trigger, run mode, final status, phase, tasks, source/PDF/conversion/archive/LFS/rights metrics, period watermarks, taxonomy state, commit decision, redacted error, and next safe action.

Metric names and headings are fixed by code. Missing values render as `nicht gemeldet`; they are never invented as zero. All text passes existing secret/e-mail/credential redaction, ANSI stripping, control-character removal, line folding, length limits, and Markdown escaping.

CLI input may be a run manifest or a transaction envelope containing `run_manifest`. Output is stdout or an explicit file. Diagnostic retention follows final state: 14 days for success/no-op/recovered, 30 for failed, 90 for blocked. The CLI can emit this value through `GITHUB_OUTPUT` without changing its validation exit code.

### Rolling incident issue

`scripts/rki_pipeline/incident_issue.py` separates decision from transport:

- `plan_incident_issue(...)` consumes validated public status plus normalized existing marker matches.
- zero matches and threshold reached: create;
- one open match and threshold reached: update;
- one closed match and threshold reached: reopen and update;
- one open match after healing: comment and close;
- otherwise: no-op;
- more than one marker match: fail closed.

Stable marker: `<!-- desinfect:rki-pipeline-incident:v1 -->`. Dedicated label: `pipeline-incident`. Title prefix and repository are constants. Body contains only redacted status fields and deterministic recovery guidance.

Small stdlib HTTP adapter uses GitHub REST with fixed host/repository routes, API-version/Accept headers, bounded response sizes, timeouts, bounded pagination, and injected transport tests. Token is accepted only through an argument/environment boundary and never rendered.

### Workflow integration

Pipeline CLI persists the redacted failure manifest already carried by `TransactionError` before returning its existing non-zero exit. This adds evidence without changing fachlicher exit status.

Reusable pipeline workflow:

1. renders summary under `if: always()` when a manifest exists, otherwise writes a fixed unavailable-manifest diagnostic;
2. appends generated Markdown to `GITHUB_STEP_SUMMARY`;
3. uploads transaction evidence and summary under `if: always()` with state-derived retention;
4. optionally obtains a separate Wachhund token with only `issues:write` and runs incident maintenance when `ROLLING_ISSUE_ENABLED == 'true'`;
5. leaves the original failing step and job conclusion unchanged.

## Rejected Alternatives

- Direct `gh issue` shell composition: harder to unit-test and easier to expose untrusted Markdown or token-bearing diagnostics.
- New observability schema/service: not required; current run/status contracts already hold needed state.
- Cloudflare logging in P08.2: belongs to P09.

## Verification

- Injection and redaction tests for ANSI, Markdown, tokens, e-mails, URLs, multiline errors, and oversized input.
- Summary tests for success, failure, blocked, missing metrics, deterministic ordering, and retention.
- Incident tests for create/update/reopen/heal/no-op, threshold bounds, duplicate marker rejection, fixed routes, and token non-disclosure.
- CLI regression proving failed transaction output is written while exit remains non-zero.
- Workflow contract proving `if: always()`, time-limited artifact, fixed permissions, short-lived App token, and no effect on blocking exit.

