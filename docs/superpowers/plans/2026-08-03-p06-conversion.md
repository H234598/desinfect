# P06.3 PDF Validation and Conversion Implementation Plan

> Execute task-by-task with TDD, task review, and full regression gates.

**Goal:** Validate text, scan, and damaged PDFs deterministically; preserve source bytes; materialize traceable page-marked text; expose OCR uncertainty.

**Architecture:** `pdf_validation.py` owns shared byte validation and a bounded no-shell process runner. `conversion/` owns immutable tool/runtime evidence, text extraction, quality gating, and optional OCR. One orchestrator writes output and Conversion Manifest 1.1 atomically only after all checks pass. `scripts.rki_pipeline.cli` is a thin router to existing and conversion CLIs.

**Baseline:** `origin/main@0e4fe01624d45b750f8a8dd4abf3b5d160e7e46e`; `540 passed`.

**Constraints:** Source bytes never change. No shell execution or machine paths in manifests. Fixed locale/environment and ordered argv. Ceilings: 256 MiB source, 2,000 pages, 100 MP/page, 120 s wall/CPU per tool, 2 GiB address space, 256 descriptors, 512 MiB generated file/stdout, 1 MiB stderr. OCR always yields `needs_review`. Missing OCR tools stay visible. Same fingerprint plus matching output SHA skips all writes and preserves mtime. `codegraph` unavailable; structure traced once with `rg` and Context Mode.

## Task 1: Shared PDF validation and bounded tool execution

**Files:**

- Create: `scripts/rki_pipeline/pdf_validation.py`
- Create: `tests/test_pdf_validation.py`
- Modify: `scripts/rki_grabber/download.py`
- Modify: `tests/test_download_security.py`

**RED:** Test regular-file and size bounds, `%PDF-`/terminal `%%EOF`, streaming hashes, parser-open/page-count/encryption failures, fixed environment/argv, timeout and output limits, process-group termination, symlink rejection, and unchanged downloader error contracts.

**GREEN:** Extract one reusable descriptor-based byte validator. Keep downloader-specific exception mapping. Add injected runner for tests and a production Poppler runner using argument arrays, private marked temp roots, resource limits, bounded capture, and cleanup restricted to owned roots.

**Verify:**

```bash
.venv/bin/python -m pytest -q tests/test_pdf_validation.py tests/test_download_security.py
```

**Commit:** `feat(p06): add bounded PDF validation`

## Task 2: Conversion Manifest 1.1 and deterministic evidence

**Files:**

- Modify: `schemas/conversion-manifest.schema.json`
- Modify: `config/schema-registry.json`
- Modify: `scripts/rki_pipeline/schema_registry.py`
- Create: `scripts/rki_pipeline/conversion/__init__.py`
- Create: `scripts/rki_pipeline/conversion/base.py`
- Create: `scripts/rki_pipeline/conversion/quality.py`
- Create: `tests/fixtures/schemas/conversion-manifest-v1.0.json`
- Modify: `tests/fixtures/manifest.json`
- Modify: `tests/test_schema_migrations.py`
- Modify: `tests/test_schemas.py`
- Create: `tests/test_conversion.py`

**RED:** Test strict 1.1 fields, deterministic/non-mutating 1.0→1.1 migration with `legacy_needs_review`, nullable legacy evidence, ordered toolchain evidence, canonical fingerprint sensitivity to executable/library/font/runtime/options drift, no machine paths, quality thresholds, and unknown-version rejection.

**GREEN:** Keep all 1.0 fields. Add required `conversion_id`, `bitstream_id`, page count, ordered toolchain/runtime, `fingerprint_sha256`, nullable storage reference, and provenance state. Reuse canonical JSON/SHA helpers. Legacy migration sets new evidence to null plus `legacy_needs_review`; it invents no evidence or authorization.

**Verify:**

```bash
.venv/bin/python -m pytest -q tests/test_schema_migrations.py tests/test_schemas.py tests/test_conversion.py
.venv/bin/python scripts/validate_schemas.py
.venv/bin/python scripts/validate_fixture_manifest.py
```

**Commit:** `feat(p06): version conversion evidence`

## Task 3: Text extraction, quality gate, OCR, and atomic orchestration

**Files:**

- Create: `scripts/rki_pipeline/conversion/pdftotext.py`
- Create: `scripts/rki_pipeline/conversion/ocr.py`
- Create: `scripts/rki_pipeline/conversion/service.py`
- Modify: `scripts/rki_pipeline/paths.py`
- Modify: `scripts/rki_pipeline/storage/base.py`
- Modify: `tests/test_conversion.py`

**RED:** Test exactly one `<!-- rki-page: N -->` marker per page, fixed Poppler options, quality-triggered OCR only, `deu+eng`, DPI/color/PSM/OEM/tessdata evidence, page/pixel limits, missing OCR tools, forced `needs_review`, source immutability, rights matrix and intra-call revocation, symlink/path escape, rollback after write/fsync/`KeyboardInterrupt`, tamper-aware idempotence, and no-write/mtime preservation.

**GREEN:** Resolve canonical Markdown path and authorize through exact `RightsStorageAuthorizer`; reauthorize before temp write. Convert in an owned sentinel-marked tree, normalize form-feed pages deterministically, evaluate coverage/text thresholds, run per-page OCR only when needed, validate output bounds, then publish derived text and manifest as one rollback-safe pair. Existing manifest and output SHA/size must match before skip. Failed conversion leaves original and pre-existing targets untouched and returns explicit `failed`/`needs_review` evidence.

**Verify:**

```bash
.venv/bin/python -m pytest -q tests/test_pdf_validation.py tests/test_conversion.py
```

**Commit:** `feat(p06): convert PDFs deterministically`

## Task 4: Thin CLI, real Poppler smoke, operations docs, CI, and delivery

**Files:**

- Create: `scripts/rki_pipeline/conversion_cli.py`
- Create: `scripts/rki_pipeline/cli.py`
- Modify: `tests/test_cli_entrypoints.py`
- Modify: `tests/test_conversion.py`
- Create: `docs/Wartung/PDF-Konvertierung.md`
- Modify: `.github/workflows/p00-baseline.yml`
- Modify: `.github/workflows/rki-pipeline.yml`

**RED/GREEN:** Route exact `python -m scripts.rki_pipeline.cli convert --fixture ... --mode materialize` without touching P05 `pipeline_cli.py` or duplicating its logic. Permit only `RunMode.MATERIALIZE` and `TEMP_FILE`; no repo/Git/LFS/apply effects. Test materialize, missing-tool, failed, `needs_review`, and skipped exit/output contracts. Add one real installed-Poppler smoke using a generated minimal text PDF; keep Tesseract optional and visibly gated. Document reviewed package/license set, resource ceilings, fingerprint inputs, rollback, and review flow. Add focused CI gate.

**Full gate:**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/node/*.test.mjs
.venv/bin/python -m compileall -q scripts tests
.venv/bin/python scripts/validate_all_baseline.py
.venv/bin/python scripts/validate_p01_foundation.py
.venv/bin/python scripts/validate_p02_contracts.py
.venv/bin/python scripts/validate_p03_grabber.py
.venv/bin/python scripts/validate_p04_storage.py
.venv/bin/python scripts/validate_p05_dispatcher.py
.venv/bin/python scripts/validate_rights_register.py
.venv/bin/python scripts/validate_ci_mutation_safety.py
.venv/bin/python scripts/validate_dependency_locks.py
.venv/bin/python scripts/validate_schemas.py
.venv/bin/python scripts/validate_fixture_manifest.py
git diff --check
git status --short
```

**Review/delivery:** Whole-branch security and requirements review, fix rounds, Draft PR, CodeRabbit resolution, ready, squash merge after all checks pass.

**Commit:** `docs(p06): document PDF conversion`
