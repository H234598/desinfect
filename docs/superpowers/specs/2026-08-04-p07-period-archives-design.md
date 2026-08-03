# P07.2 Period Archive Design

**Date:** 2026-08-04  
**Scope:** P07.2 only  
**Approval basis:** P07.2 blueprint, presented option A, and standing user approval for autonomous blueprint execution without routine confirmation

## Goal

Build deterministic weekly, monthly, and yearly PDF/Markdown archive products for every explicitly due or affected closed period. A late document rebuilds exactly its historical week, month, and year. Monthly indexes and period manifests remain canonical, backend-neutral, and idempotent.

P07.3 reconciliation remains out of scope. P07.2 does not publish commits, push storage objects, mutate canonical source files, or change scheduler semantics.

## Considered approaches

### 1. Separate aggregation layer and period-manifest contract — selected

Add `aggregation.py` above the existing P05 due-task and P07.1 archive contracts. It plans periods, joins the validated manifest graph to authorized local objects, renders monthly indexes and period manifests, and delegates every ZIP bundle to `materialize_archive`.

Benefits:

- no duplicate ZIP, rights, validation, or atomic-publication code;
- P07.1 sidecars remain backward compatible;
- period selection is pure and independently testable;
- one versioned contract represents document/archive relationships.

Cost: one new schema contract and fixture are required.

### 2. Extend the P07.1 archive sidecar — rejected

This saves one schema name but changes a shipped `1.0.0` contract, mixes single-ZIP identity with multi-archive period relationships, and requires migration logic for existing bundles.

### 3. Put selection and indexing into `archive.py` — rejected

This avoids a module but couples period policy, manifest joins, Markdown rendering, and ZIP byte mechanics. It also contradicts the blueprint's explicit `aggregation.py` boundary.

## Inputs and boundaries

`plan_period_archives` consumes only validated, typed inputs:

- aware `as_of` instant;
- P05 `DueTask` values for `week`, `month`, and `year`;
- grabber `affected_periods` mapping;
- validated P06 `ManifestGraph`;
- exact mapping of canonical logical path to P04 `PreparedObject`.

The manifest graph is the metadata authority. `PreparedObject` is the local-byte and fresh-rights authority. A path must resolve to both with identical artifact identity, size, SHA-256, source, document/conversion linkage, visibility, and rights state. Missing, extra, stale, superseded, or ambiguous joins fail closed.

No repository file is opened by an untrusted manifest path. Callers materialize verified objects below a temporary root first, as in P04/P06.

## Period model

`PeriodKind` contains `week`, `month`, and `year`. `PeriodRef` stores one canonical value:

- week: `YYYY-Www`, ISO week;
- month: `YYYY-MM`;
- year: `YYYY`.

Every period derives an inclusive local start date, inclusive local end date, and end-exclusive midnight in `Europe/Berlin`. ZIP `source_date_epoch` is the UTC epoch of that end-exclusive instant. It therefore stays stable across reruns and handles DST without using run time.

The planner validates that every due or affected period is fully closed at `as_of`. It unions due tasks with `affected_periods.weeks`, `.months`, and `.years`, deduplicates, and sorts by kind then chronological start. Future, malformed, unknown, boolean-year, or nonclosed periods fail before archive selection.

P05 remains responsible for catch-up and watermarks. P07.2 does not independently invent missed periods; it consumes P05 due tasks. Late-arrival periods are an additional explicit union.

## Document selection

For each planned period, select current non-superseded document manifests whose `canonical_periods` value matches exactly. Join:

- source manifest for title, handle, DOI, publication date, and source identity;
- conversion manifest for Markdown state and output identity;
- storage-reference manifest for canonical PDF/Markdown path, checksum, byte count, visibility, and backend-neutral artifact ID;
- `PreparedObject` for the actual authorized bytes.

PDF and Markdown payloads are separate. A missing Markdown conversion does not suppress the PDF archive. It is represented in the monthly index as its exact conversion state. No archive is emitted when its payload set is empty.

An archive contains payload files, never another ZIP. Year archives therefore contain the selected document PDF or Markdown files directly.

## Names and output layout

Weekly bundle names include the concrete date interval:

- `RKI-Einzelartikel-2026-07-06_bis_2026-07-12-PDF`
- `RKI-Einzelartikel-2026-07-06_bis_2026-07-12-Markdown`

Monthly and yearly bundles use stable period and format names below:

- `rki/Bulletins/Monate/YYYY/MM/ZIP/`
- `rki/Bulletins/Monate/YYYY/MM/Markdown/index.md`
- `rki/Bulletins/Jahre/YYYY/ZIP/`
- `rki/Bulletins/Manifeste/Archive/<period-kind>/<period>.json`

Each bundle remains the P07.1 directory containing `archive.zip` and `archive-manifest.json`. Archive member names reuse canonical document basenames, preventing absolute paths or repository-layout leakage inside ZIPs.

## Monthly index

`render_month_index` emits stable UTF-8 Markdown with LF endings. Rows sort by publication date, document ID, then source ID and contain:

- article count;
- publication date;
- title;
- RKI handle;
- DOI or `—`;
- relative PDF and Markdown links when present;
- exact conversion state;
- PDF and Markdown SHA-256 values when present;
- links to every overlapping weekly PDF/Markdown archive.

Text is escaped for Markdown tables. Links are derived from canonical paths and period output paths; raw source text cannot inject paths or HTML.

## Period manifest

Add registered JSON Schema contract `period-archive-manifest` version `1.0.0`. One canonical JSON file represents one period and contains:

- kind, canonical period, Berlin start/end dates, timezone, and stable `source_date_epoch`;
- ordered current document versions with source ID, publication date, and PDF/Markdown artifact IDs/checksums;
- ordered archive references with archive ID, kind, relative bundle path, input fingerprint, output SHA-256, byte count, and nullable storage-reference ID;
- for year periods, ordered references to available month manifests;
- canonical input fingerprint over all fields except storage-reference IDs.

References use artifact IDs and repository-relative logical paths, never backend URLs or backend-specific object keys. P04 storage references remain the resolver authority.

P07.1 `archive-manifest` stays unchanged.

## Materialization

`materialize_period_archives` accepts only `RunMode.MATERIALIZE`, matching `temp_root` and `EffectLedger`. It processes one `AggregationPlan` under a staging directory:

1. reauthorize and revalidate every `PreparedObject`;
2. call P07.1 `materialize_archive` for every nonempty archive spec;
3. render the month index and period manifest from returned archive identities;
4. validate the period manifest against the registered schema;
5. atomically publish the complete generated tree.

Identical inputs return `changed=False`, preserve mtimes, and add no ledger events. Any pre-publication failure removes staging and truncates tentative events. A previously valid tree remains unchanged.

P07.2 materializes only below a temporary root. P05's transaction writer owns repository apply/commit/push.

## CLI

`python -m scripts.rki_pipeline.cli aggregate --as-of 2026-01-01T05:00:00Z --mode plan` uses an offline synthetic fixture and prints one canonical JSON plan to stdout.

Accepted modes:

- `plan`: read-only; no filesystem mutation;
- `materialize`: isolated temporary-root smoke, strict validation, stable evidence output.

`apply` is rejected. Repository mutation must pass through the P05 transaction.

## Error model

- `AggregationError`: malformed or inconsistent aggregation contract.
- `PeriodSelectionError`: invalid, future, or nonclosed due/affected period.
- `PeriodManifestError`: graph, schema, fingerprint, or archive-reference mismatch.

Errors identify IDs and contract fields, never payload bytes, tokens, or absolute host paths.

## Tests

`tests/test_period_archives.py` covers:

- Berlin week/month/year boundaries, ISO-year rollover, leap year, and DST;
- deterministic due/affected union and stable ordering;
- malformed, future, boolean-year, and nonclosed-period rejection;
- exact current-document selection and superseded-version exclusion;
- independent PDF/Markdown output and no empty archives;
- monthly index escaping, fields, checksums, relative links, and order;
- yearly payload files instead of nested month ZIPs;
- canonical period manifest, backend-neutral references, and month links;
- late arrival changing only its historical week/month/year;
- materialize no-op, corruption replacement, rights failure, and rollback;
- plan/materialize CLI determinism and zero repository mutation.

Schema tests register and validate the new contract. Existing archive, manifest, storage, dispatcher, conversion, and full suites remain green.

## Documentation and CI

- Add `docs/Wartung/Periodenarchive.md` with period boundaries, paths, late arrivals, no-op, rollback, and recovery.
- Update `rki/Bulletins/README.md` with generated monthly/yearly layout.
- Add focused period-archive tests and aggregate CLI smoke to the existing baseline workflow.

## Self-review

- No placeholders or unresolved choices.
- P07.2 scope is isolated from P07.3 and repository apply.
- Period, graph, rights, storage, and ZIP authorities are explicit.
- P07.1 and existing schema contracts remain backward compatible.
- No new dependency; `zoneinfo`, JSON, hashing, paths, and dataclasses use the standard library.
