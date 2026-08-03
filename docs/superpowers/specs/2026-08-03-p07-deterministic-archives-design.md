# P07.1 Deterministic Archive Design

**Date:** 2026-08-03  
**Scope:** P07.1 only  
**Approval basis:** Existing P07.1 blueprint and standing user approval for autonomous blueprint execution

## Goal

Identical authorized inputs and build parameters produce byte-identical ZIP files. Invalid paths, links, nested ZIPs, oversized inputs, excessive decompression ratios, stale rights decisions, and partial publication fail closed.

P07.2 period selection, late-arrival aggregation, and final repository paths remain out of scope. P07.3 reconciliation remains out of scope.

## Considered approaches

### 1. Standard-library ZIP with stored entries — selected

Use `zipfile` with `ZIP_STORED`, explicit `ZipInfo`, normalized UTC timestamps, fixed regular-file modes, empty extra/comment fields, and sorted NFC POSIX names.

Benefits:

- byte stability does not depend on zlib version;
- PDFs gain little from a second compression pass;
- no dependency or external executable;
- validation remains straightforward.

Cost: Markdown archives are larger. A pinned deterministic compressor may replace `ZIP_STORED` only after measured storage pressure and a format-version bump.

### 2. Standard-library deterministic Deflate — rejected

Smaller Markdown archives, but bytes depend on pinned zlib behavior. Same logical input could drift between runners or runtime upgrades.

### 3. External `zip`/libarchive tool — rejected

Adds executable/version supply-chain state and makes error/metadata normalization less inspectable.

## Components

### Archive inputs

`ArchiveEntry` binds:

- canonical archive member path;
- a `PreparedObject` below an explicit temporary root;
- immutable byte count and SHA-256 already carried by that object.

Every entry is reauthorized with a fresh `RightsStorageAuthorizer` before a no-op decision or build. Entries in one archive must have one visibility. Portable path collisions, reserved metadata names, and `.zip` payloads are rejected.

`ArchiveSpec` binds archive ID, period, schema kind, visibility, entries, and `SOURCE_DATE_EPOCH`. The normalized timestamp is included in the input fingerprint because it affects output bytes.

### Deterministic renderer

The renderer writes one ZIP to a caller-provided staging path. It never extracts an archive and never mutates repository sources.

Member order is the lexical order of NFC POSIX names across payloads and these required metadata members:

- `MANIFEST.json`
- `README.md`
- `SHA256SUMS.txt`

Payload manifest entries contain path, byte count, and SHA-256. `SHA256SUMS.txt` covers payload members. Metadata does not attempt a self-referential checksum.

All members use:

- `ZIP_STORED`;
- Unix regular-file mode `0644`;
- no UID/GID, comment, or extra fields;
- UTC `SOURCE_DATE_EPOCH`, clamped to the ZIP range and rounded down to an even second.

Input fingerprint is canonical SHA-256 over archive format version, identity, kind, period, visibility, normalized timestamp, and ordered payload path/size/SHA triples.

### Strict validator

Validation happens without extraction. It rejects:

- invalid, absolute, traversing, non-NFC, duplicate, or colliding names;
- directory, symlink, device, encrypted, commented, or nested-ZIP members;
- missing or duplicate required metadata;
- unsupported compression;
- configured entry, member, archive, total-uncompressed, or compression-ratio limits;
- malformed/noncanonical metadata JSON or checksum lines;
- CRC, size, or SHA-256 mismatches;
- mismatch between internal manifest, checksums, payload members, and expected build result.

### Atomic materialization

One generated bundle directory contains the ZIP and its schema-valid archive-manifest sidecar. `staged_directory` publishes the complete bundle atomically beneath `temp_root`.

If an existing sidecar has the expected input fingerprint, the publisher reauthorizes every input, strictly validates the existing ZIP, verifies its output SHA-256, and returns a no-op without changing mtimes or ledger events.

If existing output is corrupt or stale, a complete staged bundle replaces it. Any failure discards staging, truncates tentative ledger events, and preserves the previous bundle.

Only `RunMode.MATERIALIZE` is implemented in P07.1. Publication to repository or storage backends belongs to P07.2.

### CLI smoke

`python -m scripts.rki_pipeline.cli build-archive --fixture pilot --mode materialize` routes to the archive module. The fixture build uses an isolated temporary directory, validates its output, prints stable JSON evidence, and leaves no repository mutation.

## Error model

- `ArchiveError`: base contract failure.
- `ArchiveSecurityError`: unsafe member/source or limit violation.
- `ArchiveIntegrityError`: byte, manifest, checksum, or rights-bound identity mismatch.

Messages identify the failed contract without emitting payload bytes or secrets.

## Tests

`tests/test_archives.py` covers:

- two builds with identical SHA-256 and exact member metadata/order;
- input fingerprint and mtime-preserving no-op;
- timestamp minimum/maximum/even-second normalization;
- Unicode/case collision, traversal, absolute path, backslash, reserved name, symlink, and nested ZIP rejection;
- entry/member/total/archive size and decompression-ratio limits;
- stale rights and visibility mixing;
- corrupt ZIP/manifest self-healing and staged rollback;
- CLI fixture smoke with zero repository mutation.

Full existing validators and test suites remain green.

## Documentation and CI

- Add `docs/Wartung/Archivformat.md` with format, limits, no-op, verification, and rollback behavior.
- Run focused deterministic/security archive tests and CLI smoke in CI.
- Record ADHS `build_exports.py` as design provenance only; no source file was available locally and no code is copied.

## Self-review

- No placeholders or unresolved choices.
- P07.2/P07.3 scope explicitly excluded.
- Data flow, rights boundary, rollback, limits, and verification agree.
- No new dependency or speculative backend abstraction.
