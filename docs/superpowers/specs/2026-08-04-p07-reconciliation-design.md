# P07.3 Reconciliation Design

**Date:** 2026-08-04
**Scope:** P07.3 only
**Approval basis:** P07.3 blueprint, presented option A, and standing user approval for autonomous blueprint execution without routine confirmation

## Goal

Detect remote-source drift, missing or corrupt local objects, orphaned storage references, changed rights decisions, and incomplete period archives reproducibly. Every run produces deterministic findings and a schema-valid aggregate report. Any unresolved finding blocks success and leaves the prior successful reconciliation watermark and report untouched.

P07.3 does not backfill data, repair findings, mutate canonical sources, publish commits, or implement the P13 readiness gate. P05 remains the scheduler and transaction authority. P07.1/P07.2 remain the archive-byte and period-manifest authorities.

## Considered approaches

### 1. Manifest-first planner with existing adapters — selected

Add `reconciliation.py` above the validated P06 `ManifestGraph`, P04 `StorageAdapter`, P06 rights authority, and P07 archive/period validators. Remote metadata is supplied as a bounded snapshot. Candidate content is loaded only through one injected callback after metadata establishes drift risk.

Benefits:

- reuses existing validation, rights, storage, LFS, and archive contracts;
- keeps network access outside deterministic comparison logic;
- supports all storage backends without backend branches;
- makes blind-redownload prevention directly testable;
- keeps blocked diagnostics separate from canonical successful reports.

Cost: caller must prepare one validated remote metadata snapshot and backend adapter mapping.

### 2. Monolithic repository, LFS, and HTTP scanner — rejected

This avoids an input model but duplicates adapter security checks, couples comparison logic to Git LFS and HTTP, and makes fixture execution dependent on network-shaped behavior.

### 3. Metadata-only snapshot comparison — rejected

This is smaller but cannot prove local hashes, LFS objects, archive bundles, rights decisions, or period completeness. It does not satisfy `MUSS-21`.

## Inputs and authorities

`plan_reconciliation` consumes exact validated inputs:

- aware UTC `as_of`;
- inclusive `from_year` and `to_year`;
- P06 `LoadedManifestCatalog`, retaining both `ManifestGraph` and canonical source-manifest bytes;
- deterministic tuple of remote `ArtifactRecord` metadata;
- exact mapping from P04 `StorageBackend` to `StorageAdapter`;
- canonical P06 `RightsAuthority` and `RightsPolicy`;
- root containing published period manifests and archive bundles;
- optional candidate-content loader used only for justified remote drift candidates.

Authority remains split:

- remote snapshot: current source handle, version, URLs, ETag, Last-Modified, rights metadata, and optional current content hash;
- source/document/conversion manifests: canonical identity and provenance;
- storage-reference manifests: expected backend, object identity, size, hash, visibility, rights state, and logical path;
- storage adapters: existence and byte/object integrity, including LFS pointer and object verification;
- rights authority: current decision hash and publication state;
- P07 validators: period-manifest and archive-bundle integrity.

Inputs outside the requested year scope are ignored. Duplicate remote identities, invalid scope, naive timestamps, unsupported backends, malformed manifest links, and symlinked period roots fail before comparison.

## Finding model

A `ReconciliationFinding` has a stable code, subject kind, subject ID, optional canonical relative path, and bounded diagnostic message. Findings sort by code, subject kind, subject ID, then path. Payload bytes, tokens, absolute paths, and remote response bodies never enter a finding.

Stable detail codes:

- `new`: remote source/bitstream does not exist in the local manifest graph;
- `changed`: known source metadata, version, URL, content identity, local bytes, pointer, manifest, or archive differs;
- `missing_remote`: current local source/bitstream is absent from the remote snapshot;
- `missing_local`: required storage object, local file, period manifest, or archive is absent;
- `orphan`: backend reference/object is listed but not reachable from the manifest graph;
- `rights_changed`: current rights decision differs from persisted provenance;
- `ok`: one fully verified source/bitstream has no finding.

A subject may have multiple non-`ok` findings only when they describe distinct contract failures. Exact duplicate keys fail closed during planning.

The existing `reconciliation-report` 1.0.0 schema remains unchanged. Its aggregate has no `new` field, so a `new` detail finding increments `missing_local`: remote existence proves the corresponding local record is missing. `unresolved` equals the count of every non-`ok` detail finding. `ok` counts only fully verified source/bitstream subjects.

## Remote comparison and candidate loading

Remote and local sources join by exact `(source_id, bitstream_id)`. Local comparison includes only source manifests referenced by current, non-superseded document manifests; a historical superseded version absent from the current remote snapshot is not `missing_remote`. Metadata comparison covers source/document version, item and bitstream URLs, ETag, Last-Modified, publication date, and source-provided content hash when present.

A candidate content load is justified only when:

- a known remote record changes version, bitstream identity, URL, ETag, Last-Modified, or supplied hash; and
- the record has a PDF URL and is not already conclusively `new` or `missing_remote`.

The loader returns a verified size/SHA-256 identity from a temporary object. Reconciliation never accepts raw caller claims as downloaded-byte evidence. Stable metadata never invokes the loader. A justified candidate with no loader, or a loader failure, creates a bounded `changed` finding and blocks success; it does not replace local bytes.

A metadata change remains `changed` even when candidate bytes match, because provenance drift must be reviewed and manifested.

## Local storage and orphan checks

Each storage manifest is converted once to the existing typed `StorageReference`. The matching adapter:

1. authorizes verification using existing rights/storage policy;
2. verifies path, pointer/object, size, and SHA-256;
3. lists backend references for orphan comparison.

Missing targets classify as `missing_local`. Existing but corrupt, mismatched, or unauthorized targets classify as `changed` or `rights_changed` according to root cause. Backend references absent from the manifest graph classify as `orphan`.

No direct Git-LFS object traversal is added. `LfsStorageAdapter.verify` and `list_references` remain the LFS authority. Remote/release/object adapters keep their existing verification semantics.

## Rights comparison

For every current source hash, `resolve_rights` reloads the canonical register. Persisted source and storage decision hashes, rights states, visibility, and publication eligibility must agree with the current decision.

Any disagreement produces `rights_changed`. Metadata-only, restricted, takedown, or legacy provenance remains fail-closed. Reconciliation records drift but never updates the rights register.

## Period and archive completeness

For each current document in scope with a publication date, derive its exact ISO week, calendar month, and calendar year. The published period root must contain valid P07.2 manifests for all three periods. Each manifest must:

- validate against `period-archive-manifest`;
- contain the exact current `(document_id, bitstream_id)`;
- reference every nonempty PDF or Markdown product supported by the document/storage graph without requiring an unavailable format;
- point to existing bundles whose sidecar, ZIP, size, output SHA-256, input fingerprint, and member identities pass P07.1 validation;
- retain valid month-to-week and year-to-month relationships.

Absent required manifest/bundle is `missing_local`; present but inconsistent/corrupt data is `changed`. P07.3 calls public P07 validators and does not implement a second ZIP reader.

Documents lacking a full publication date can be checked at year level only. This limitation is explicit in findings and does not invent week/month membership.

## Result and report

`ReconciliationResult` contains:

- deterministic findings;
- exact aggregate counts;
- `success` or `blocked` conclusion;
- canonical source-manifest SHA-256;
- report payload;
- nullable `successful_at`, equal to `as_of` only for `success`.

The report payload is exactly schema version `1.0.0`:

- inclusive year scope;
- canonical UTC `as_of`;
- aggregate counts;
- conclusion;
- SHA-256 of canonical `Quellen/manifest.jsonl` bytes.

P07.3 emits only `success` and `blocked`. Existing `degraded` and `failed` schema values remain reserved for later orchestration/runtime failures.

## Materialization and rollback

`materialize_reconciliation` accepts only `RunMode.MATERIALIZE`, a matching temporary root, and an `EffectLedger`.

For `success`, it validates the report and atomically stages:

`rki/Bulletins/Manifeste/Reconciliation/reconciliation-YYYYMMDDTHHMMSSZ.json`

Identical report identity is a no-op. A different successful timestamp creates a new immutable report; prior reports remain.

For `blocked`, no canonical report is staged. The returned typed findings are the transient diagnosis, `successful_at` is `None`, and the ledger remains free of persistent write effects. Any validation or staging failure removes tentative output and preserves every prior successful report.

P05 may advance `periods.last_reconciliation_at` only from non-null `successful_at`. P07.3 does not directly edit runtime status.

## CLI and fixture

`python -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize` uses an offline fixture containing:

- canonical manifest catalog;
- remote metadata snapshot;
- local storage objects/references;
- period manifests and archive bundles;
- current rights policy/register.

Accepted modes:

- `plan`: read-only canonical JSON evidence, including deterministic findings;
- `materialize`: isolated temporary-root report smoke and canonical JSON evidence.

`apply` is rejected. Fixture parsing is strict, bounded, symlink-safe, and independent of wall clock, timezone, network, backend credentials, and repository mutation.

## Error model

- `ReconciliationError`: malformed top-level contract or unsupported operation.
- `RemoteSnapshotError`: duplicate, invalid, or inconsistent remote metadata.
- `ReconciliationIntegrityError`: graph, storage, rights, period, archive, count, fingerprint, or report mismatch.

Expected drift becomes a finding. Invalid input, unsafe paths, ambiguous identity, adapter contract failure, and internal inconsistency raise an error. CLI maps expected errors to stable path-free stderr and exit code 1; usage errors return 2 without traceback.

## Tests

`tests/test_reconciliation.py` covers:

- scope, UTC timestamp, duplicate identity, and deterministic ordering;
- every detail code and exact aggregate mapping;
- `new` counted as `missing_local`;
- metadata-stable runs making zero candidate-load calls;
- changed metadata making exactly one bounded candidate-load call;
- candidate same-hash, changed-hash, and loader-failure behavior;
- missing and corrupt local files, LFS pointers/objects, and backend references;
- orphan detection through adapter inventories;
- current, changed, restricted, takedown, and missing rights decisions;
- missing, corrupt, and complete week/month/year manifests and archive bundles;
- partial-date year-only behavior;
- exact unresolved count and `successful_at` watermark eligibility;
- schema-valid canonical report and canonical source-manifest hash;
- materialize success, immutable report naming, no-op, blocked no-write, and rollback;
- plan/materialize CLI determinism and zero repository mutation.

Existing manifest, rights, storage, archive, period, dispatcher, schema, and full suites remain green.

## Documentation and CI

- Add `docs/Wartung/Reconciliation.md` with finding semantics, candidate loading, report paths, watermarks, recovery, and operator actions.
- Add `runbooks/RKI-SOURCE-CHANGED.md` with safe triage steps and explicit non-automatic repair.
- Register the focused test and offline CLI smoke in the existing baseline workflow without network or persistent mutation.

## Self-review

- No placeholders or unresolved choices.
- P07.3 is isolated from backfill, automated repair, readiness, and repository apply.
- Remote, manifest, rights, storage, period, and archive authorities are explicit.
- Existing report, archive, period, storage, and status schemas remain backward compatible.
- No new dependency; standard-library dataclasses, enums, JSON, hashing, paths, and existing project contracts suffice.
