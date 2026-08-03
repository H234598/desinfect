# P06.1 Document Identity, Paths, and Source Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build stable RKI document/bitstream identities, deterministic canonical PDF/Markdown paths, migratable Source/Document manifest 1.1 contracts, and atomic manifest builders without making any publication-rights decision.

**Architecture:** Pure identity and path modules become the only source of IDs and paths; existing grabber models/parser delegate to them. Source/Document schemas evolve one version through explicit fail-closed migrations. Manifest builders map validated grabber records into schema-valid metadata; P06.2 remains sole owner of reviewed rights authorization.

**Tech Stack:** Python 3.12+, stdlib `dataclasses`, `datetime`, `hashlib`, `urllib.parse`; JSON Schema Draft 2020-12; pytest 9.1.1; existing `io_utils`, schema registry, grabber contracts, and Git-LFS adapter.

## Global Constraints

- Canonical source host is exactly `https://edoc.rki.de`; credentials, fragments, ports, unknown query keys, duplicate query keys, and non-PDF bitstream paths fail closed.
- `document_id` derives only from RKI handle plus handle version. Title never contributes.
- `bitstream_id` is `rki-bitstream-` plus lowercase SHA-256 of canonical bitstream URL. RKI `sequence` is positive integer or explicit `null`; never silently default it.
- Parser deduplicates only identical `bitstream_id`. Distinct URL/sequence candidates remain present even with identical MD5.
- Publication date alone determines canonical ISO week/month/year. Missing full date blocks canonical path/manifest materialization; download time is never substituted.
- Canonical artifact root is existing `rki/Bulletins`. Gesamtausgaben use `Jahre/<YYYY>/PDF|Markdown`; Einzelartikel use `Einzelartikel/<YYYY>/<MM>/PDF|Markdown`.
- Every generated path is NFC POSIX, root-contained, collision-checked, Windows-reserved-name safe, and each UTF-8 component is at most 240 bytes.
- Source/Document 1.0 inputs migrate deterministically and non-mutatingly to 1.1. Legacy unknown fields become `null` plus `provenance_state=legacy_needs_review`; no metadata is invented.
- New P06.1 manifests carry `rights.state=unknown`, `basis=rights_policy_pending`, null review fields, and raw rights evidence only. They cannot authorize storage/publication.
- Tests must follow RED → observed expected failure → minimal GREEN. No network. No dependency additions.
- Requirement IDs covered: `V2-07-CONVERT-001..009`, `V2-24-RIGHTS-001`, `MUSS-29`; this package implements only their P06.1 identity/path/source-manifest slice.

---

### Task 1: Stable document and bitstream identities

**Files:**
- Create: `scripts/rki_pipeline/documents.py`
- Create: `tests/test_document_identity.py`
- Modify: `scripts/rki_grabber/models.py:13-17,152-226`
- Modify: `scripts/rki_grabber/parser.py:250-291`
- Modify: `tests/test_rki_parser.py:43-70`
- Modify: `tests/test_grabber_api.py`
- Modify: `scripts/validate_p03_grabber.py`

**Interfaces:**
- Produces: `DocumentIdentity(handle: str, source_id: str, document_id: str, version: int, supersedes: str | None)`.
- Produces: `BitstreamIdentity(canonical_url: str, bitstream_id: str, version: int | None)`.
- Produces: `document_identity(handle: str) -> DocumentIdentity`.
- Produces: `bitstream_identity(url: str) -> BitstreamIdentity`.
- Existing `ItemMetadata.source_id`, `.document_id`, `.version` delegate to `document_identity`.
- Existing `PdfCandidate` gains read-only `.bitstream_id` and `.bitstream_version` properties; serialized P03 result shape remains unchanged.

- [x] **Step 1: Write failing identity tests**

Add literal expectations to `tests/test_document_identity.py`:

```python
def test_document_identity_uses_handle_version_not_title() -> None:
    first = document_identity("176904/12345.2")
    second = document_identity("176904/12345.2")
    assert first == second == DocumentIdentity(
        handle="176904/12345.2",
        source_id="rki:176904/12345.2",
        document_id="rki-176904-12345-v2",
        version=2,
        supersedes="rki-176904-12345-v1",
    )


def test_unversioned_handle_is_explicit_version_one() -> None:
    identity = document_identity("176904/12345")
    assert identity.document_id == "rki-176904-12345-v1"
    assert identity.version == 1
    assert identity.supersedes is None


def test_bitstream_identity_canonicalizes_access_flag_without_losing_sequence() -> None:
    identity = bitstream_identity(
        "https://EDOC.RKI.DE/bitstream/handle/176904/12345.2/file.pdf"
        "?isAllowed=y&sequence=2"
    )
    assert identity.canonical_url == (
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/file.pdf?sequence=2"
    )
    assert identity.version == 2
    assert identity.bitstream_id == (
        "rki-bitstream-"
        "34798e932d2d24e4be04a2fb7c7797c7391bf238c266f86c435cc06f83e4d231"
    )
```

Before implementation, independently verify the SHA literal once with `printf %s <canonical-url> | sha256sum`; never derive it with production code in the test.

Add parametrized rejection cases for `http`, foreign host, port, credentials, fragment, duplicate/zero/noninteger `sequence`, unknown query, and non-PDF path. Each expects `DocumentIdentityError`.

- [x] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_document_identity.py
```

Expected: collection failure because `scripts.rki_pipeline.documents` does not exist.

- [x] **Step 3: Implement minimal pure identity module**

Use these exact public shapes:

```python
class DocumentIdentityError(ValueError):
    """RKI identity input is malformed or ambiguous."""


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    handle: str
    source_id: str
    document_id: str
    version: int
    supersedes: str | None


@dataclass(frozen=True, slots=True)
class BitstreamIdentity:
    canonical_url: str
    bitstream_id: str
    version: int | None
```

Canonicalize only query keys `sequence` and `isAllowed`; require at most one each. `isAllowed` must be `y` when present and is excluded from identity URL. Normalize path by `unquote` then `quote(..., safe="/-._~")`. Validate path with the existing numeric-handle PDF shape. Sort retained identity query pairs with `urlencode`.

- [x] **Step 4: Verify GREEN and mutation boundaries**

Run:

```bash
.venv/bin/pytest -q tests/test_document_identity.py
```

Expected: all identity tests pass. Mentally mutate host check, sequence validation, and handle version; named tests must fail.

- [x] **Step 5: Write failing grabber-delegation and candidate-preservation tests**

Change the P03 fixture assertion from one overwritten candidate to two retained candidates:

```python
assert [candidate.bitstream_version for candidate in metadata.pdfs] == [1, 2]
assert len({candidate.bitstream_id for candidate in metadata.pdfs}) == 2
assert {candidate.expected_md5 for candidate in metadata.pdfs} == {
    "397039b5b63ce567c48e787bbb3e18ae"
}
```

Add an inline HTML case containing the exact same canonical URL twice; assert one candidate remains. Add a conflicting-MD5 duplicate case; assert `ValueError` instead of last-write-wins.

- [x] **Step 6: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_rki_parser.py
```

Expected: existing parser returns one candidate for sequence 1/2 and fails the new preservation assertion.

- [x] **Step 7: Delegate grabber properties and fix parser deduplication**

Replace duplicated handle parsing in `ItemMetadata` with `document_identity(self.item_handle)`. Add `PdfCandidate` properties that call `bitstream_identity(self.url)`. In `_pdf_candidates`, construct candidate first and key `found` by `candidate.bitstream_id`; an exact duplicate with a different non-null MD5 raises `ValueError`. Return candidates sorted by `(bitstream_version is None, bitstream_version or 0, bitstream_id)`: positive RKI sequences ascend first, sequence-less candidates follow, and the ID breaks ties.

- [x] **Step 8: Verify Task 1 and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_document_identity.py tests/test_rki_parser.py tests/test_grabber_models.py tests/test_grabber_api.py
```

Expected: all selected tests pass.

Commit:

```bash
git add scripts/rki_pipeline/documents.py scripts/rki_grabber/models.py scripts/rki_grabber/parser.py scripts/validate_p03_grabber.py tests/test_document_identity.py tests/test_rki_parser.py tests/test_grabber_api.py
git commit -m "feat(p06): add stable RKI identities"
```

---

### Task 2: Canonical portable paths and grabber integration

**Files:**
- Create: `scripts/rki_pipeline/paths.py`
- Create: `tests/test_paths.py`
- Modify: `scripts/rki_grabber/parser.py:328-363`
- Modify: `tests/test_rki_parser.py`
- Modify: `tests/test_grabber_api.py`
- Modify: `.gitattributes`
- Modify: `scripts/rki_pipeline/storage/lfs.py:20-28`
- Modify: `tests/test_storage_lfs.py`
- Modify: `tests/test_storage_cli.py`
- Modify: `docs/Wartung/RunModes-und-Storage.md`

**Interfaces:**
- Consumes: Task 1 `DocumentIdentity`, `BitstreamIdentity`.
- Produces: `DocumentType(StrEnum)` values `gesamtausgabe`, `einzelartikel`.
- Produces: `CanonicalDocumentPaths(pdf: str, markdown: str)` relative below `rki/Bulletins`.
- Produces: `canonical_document_paths(*, document_id: str, bitstream_id: str, document_type: DocumentType, publication_date: str) -> CanonicalDocumentPaths`.
- Produces: `repository_document_paths(*, document_id: str, bitstream_id: str, document_type: DocumentType, publication_date: str) -> CanonicalDocumentPaths`, prefixing existing `CANONICAL_ARTIFACT_ROOT` exactly once.

- [ ] **Step 1: Write failing exact-path and boundary tests**

Use hand-derived literals:

```python
def test_issue_paths_use_publication_year_and_blueprint_markdown_directory() -> None:
    paths = canonical_document_paths(
        document_id="rki-176904-12345-v2",
        bitstream_id="rki-bitstream-" + "a" * 64,
        document_type=DocumentType.ISSUE,
        publication_date="1996-03-22",
    )
    stem = "1996-03-22_gesamtausgabe_rki-176904-12345-v2_rki-bitstream-" + "a" * 64
    assert paths.pdf == f"Jahre/1996/PDF/{stem}.pdf"
    assert paths.markdown == f"Jahre/1996/Markdown/{stem}.md"


def test_article_paths_use_publication_month() -> None:
    paths = canonical_document_paths(
        document_id="rki-176904-88-v1",
        bitstream_id="rki-bitstream-" + "b" * 64,
        document_type=DocumentType.ARTICLE,
        publication_date="2000-01-01",
    )
    assert paths.pdf.startswith("Einzelartikel/2000/01/PDF/2000-01-01_einzelartikel_")
```

Add cases for `1999-12-31` vs `2000-01-01`, title independence, missing/invalid date, Windows reserved components, casefold collisions, trailing dot/space, and an overlong ID. Assert every emitted component is `<=240` UTF-8 bytes and the overlong result contains full 64-hex document/bitstream hash tokens.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_paths.py
```

Expected: collection failure because `scripts.rki_pipeline.paths` does not exist.

- [ ] **Step 3: Implement path builder using existing safety primitives**

Define:

```python
class DocumentPathError(ValueError):
    """A canonical document path cannot be derived safely."""


class DocumentType(StrEnum):
    ISSUE = "gesamtausgabe"
    ARTICLE = "einzelartikel"


@dataclass(frozen=True, slots=True)
class CanonicalDocumentPaths:
    pdf: str
    markdown: str
```

Parse date with `date.fromisoformat`. Build directories exactly as above. First try readable stem. If either `.pdf`/`.md` component exceeds 240 UTF-8 bytes, use `<date>_<type>_d-<sha256(document_id)>_b-<sha256(bitstream_id)>`. Validate each component against case-insensitive `CON|PRN|AUX|NUL|COM1..COM9|LPT1..LPT9`, trailing dot/space, control characters, and 240-byte ceiling. Then run existing `normalize_posix_path`, `detect_path_collisions`, and `relative_path_beneath`/root-prefix checks.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_paths.py tests/test_paths_property.py tests/test_io_utils.py
```

Expected: all path tests pass.

- [ ] **Step 5: Write failing grabber and LFS integration tests**

Change `target_relative_path` expectation to exact `Jahre/1996/PDF/...`. In `test_grabber_api.py`, assert downloaded files stay below `output_root/Jahre/...` and two sequence candidates do not overwrite each other. Replace LFS expected Markdown tracking line with:

```text
rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text
```

- [ ] **Step 6: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_rki_parser.py tests/test_grabber_api.py tests/test_storage_lfs.py tests/test_storage_cli.py
```

Expected: legacy title-based `issues/1996` path and `Quellen/**/*.md` tracking fail literal behavior assertions.

- [ ] **Step 7: Delegate target path and migrate LFS Markdown rule**

Make `target_relative_path` map `Scope.ISSUES` to `DocumentType.ISSUE`, `Scope.ARTICLES` to `DocumentType.ARTICLE`, reject `Scope.ALL`, and return `.pdf` from `canonical_document_paths`. Remove unused legacy `safe_component` only if no caller remains. Update `.gitattributes`, `_REQUIRED_TRACKING`, tests, and operator doc to canonical `**/Markdown/**/*.md`.

- [ ] **Step 8: Verify Task 2 and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_paths.py tests/test_rki_parser.py tests/test_grabber_api.py tests/test_storage_lfs.py tests/test_storage_cli.py
.venv/bin/python scripts/validate_p03_grabber.py
.venv/bin/python scripts/validate_p04_storage.py
```

Expected: all commands exit 0.

Commit:

```bash
git add .gitattributes scripts/rki_pipeline/paths.py scripts/rki_pipeline/storage/lfs.py scripts/rki_grabber/parser.py tests/test_paths.py tests/test_rki_parser.py tests/test_grabber_api.py tests/test_storage_lfs.py tests/test_storage_cli.py docs/Wartung/RunModes-und-Storage.md
git commit -m "feat(p06): add canonical document paths"
```

---

### Task 3: Source and Document manifest 1.1 migrations

**Files:**
- Modify: `schemas/source-manifest.schema.json`
- Modify: `schemas/document-manifest.schema.json`
- Modify: `config/schema-registry.json`
- Modify: `scripts/rki_pipeline/schema_registry.py`
- Modify: `scripts/validate_schemas.py`
- Modify: `tests/test_schema_migrations.py`
- Create: `tests/fixtures/schemas/source-manifest-v1.0.json`
- Create: `tests/fixtures/schemas/document-manifest-v1.0.json`

**Interfaces:**
- Produces: `migrate_source_manifest_v1_0_to_v1_1(payload) -> dict[str, Any]`.
- Produces: `migrate_document_manifest_v1_0_to_v1_1(payload) -> dict[str, Any]`.
- Registry maps both exact `(name, "1.0.0", "1.1.0")` triples in `MIGRATIONS`.

- [ ] **Step 1: Add literal 1.0 fixtures and failing migration tests**

Source fixture uses source ID `rki:176904/12345.2`, title `Synthetic bulletin`, publication date `1996-03-22`, lowercase 64-hex `a` SHA, and `rights={state:"unknown",basis:"unreviewed",reviewed_at:null,reviewed_by:null}`. Document fixture uses `rki-176904-12345-v2`, same source ID/date, `gesamtausgabe`, `paths={pdf:"Jahre/1996/PDF/legacy.pdf",markdown:null}`, `supersedes="rki-176904-12345-v1"`.

Test each fixture for: input unchanged after migration; two migrations equal; schema version `1.1.0`; `provenance_state=legacy_needs_review`; unknown bitstream fields `null`; Source raw rights evidence all null; Document periods exactly `{week:"1996-W12",month:"1996-03",year:1996}`; current schema validation passes. Add rejection test for `0.9.0`.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schema_migrations.py
```

Expected: Source/Document registry has no supported predecessor migration.

- [ ] **Step 3: Evolve schemas and registry exactly one version**

Both `$id` and `schema_version.const` become `1.1.0`. Source adds required:

- `provenance_state`: `current|legacy_needs_review`;
- `bitstream_id`: matching `^rki-bitstream-[0-9a-f]{64}$` or null;
- `bitstream_url`: canonical RKI HTTPS URI or null;
- `bitstream_version`: integer >=1 or null;
- `rights_evidence`: strict object with required nullable `label`, `license_url`, `copyright_notice`, `open_access`;
- `decision_sha256`: lowercase SHA-256 or null;
- `same_content_as`: unique sorted array of bitstream IDs.

Document adds required:

- `provenance_state`;
- nullable `bitstream_id` and `bitstream_version`;
- `canonical_periods`: strict required `week` (`^[0-9]{4}-W(?:0[1-9]|[1-4][0-9]|5[0-3])$`), `month` (`^[0-9]{4}-(?:0[1-9]|1[0-2])$`), `year` integer 1990..9999;
- nullable `superseded_by`.

Registry entries set `current_version=1.1.0`, `previous_versions=["1.0.0"]`, and exact migration function names. Registry's own schema version remains `1.0.0`.

- [ ] **Step 4: Implement deterministic migrations**

Deep-copy input. Require exact `1.0.0`. Add only the fields above. Source migration never derives bitstream/license/OA/decision data. Document migration derives ISO week/month/year solely with `date.fromisoformat(publication_date)`. Validate result before return. Register both functions in `MIGRATIONS`.

- [ ] **Step 5: Verify GREEN and validator coverage**

Run:

```bash
.venv/bin/pytest -q tests/test_schema_migrations.py tests/test_schemas.py
.venv/bin/python scripts/validate_schemas.py
```

Expected: migrations and all twelve registered schemas pass.

- [ ] **Step 6: Extend offline schema validator and commit**

Make `validate_schemas.py` load all three predecessor fixtures (`status`, Source, Document), migrate each twice, compare equality, and validate outputs. Keep contract count exactly twelve and final message naming all three migration paths.

Run again:

```bash
.venv/bin/python scripts/validate_schemas.py
.venv/bin/pytest -q tests/test_schema_migrations.py tests/test_schemas.py
```

Commit:

```bash
git add schemas/source-manifest.schema.json schemas/document-manifest.schema.json config/schema-registry.json scripts/rki_pipeline/schema_registry.py scripts/validate_schemas.py tests/test_schema_migrations.py tests/fixtures/schemas/source-manifest-v1.0.json tests/fixtures/schemas/document-manifest-v1.0.json
git commit -m "feat(p06): evolve source and document contracts"
```

---

### Task 4: Fail-closed Source/Document manifest builders and documentation

**Files:**
- Create: `scripts/rki_pipeline/source_manifest.py`
- Create: `tests/test_source_manifest.py`
- Create: `rki/Bulletins/README.md`
- Create: `docs/Wartung/Dokumentidentitaet.md`

**Interfaces:**
- Consumes: `ArtifactRecord`, Task 1 identities, Task 2 repository paths, Task 3 schemas.
- Produces: `ManifestBuildError(ValueError)`.
- Produces: `build_source_manifest(record: ArtifactRecord, *, same_content_as: tuple[str, ...] = ()) -> dict[str, object]`.
- Produces: `build_document_manifest(record: ArtifactRecord, *, markdown_materialized: bool = False, superseded_by: str | None = None) -> dict[str, object]`.
- Produces: `build_source_manifests(records: Iterable[ArtifactRecord]) -> tuple[dict[str, object], ...]` with explicit content-alias relations.
- Produces: `write_manifest(path: Path, payload: dict[str, object], *, contract_name: str, allowed_root: Path) -> None`.

- [ ] **Step 1: Write failing builder tests with real ArtifactRecord values**

Create one downloaded issue record with full publication date, PDF URL sequence 2, SHA `a*64`, MD5, HTTP provenance, and raw rights metadata. Assert Source output contains:

```python
assert source["schema_version"] == "1.1.0"
assert source["bitstream_version"] == 2
assert source["rights"] == {
    "state": "unknown",
    "basis": "rights_policy_pending",
    "reviewed_at": None,
    "reviewed_by": None,
}
assert source["decision_sha256"] is None
assert source["rights_evidence"]["label"] == "Synthetic fixture — no publication decision"
```

Assert Document output has exact composite source/bitstream links, `supersedes`, explicit `superseded_by`, exact periods, repository-root-prefixed PDF/Markdown paths, and `markdown=None` unless `markdown_materialized=True`.

Add negative tests: planned/no-PDF/error record, missing SHA, missing publication date, missing PDF URL, unsorted/duplicate/self `same_content_as`, and invalid `superseded_by` all raise `ManifestBuildError` before any write.

Add alias test with three records: two distinct bitstream URLs share SHA `a*64`; one uses SHA `b*64`. Sorted lower bitstream ID is canonical; other same-content manifest has `same_content_as=[canonical_id]`; canonical and unrelated entries have empty arrays.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_source_manifest.py
```

Expected: collection failure because `source_manifest.py` does not exist.

- [ ] **Step 3: Implement minimal pure builders**

Require `RecordState` in `{EXISTING, DOWNLOADED, RESUMED}` and complete fields. Use `bitstream_identity(record.pdf_url)`, `document_identity(record.item_handle)`, and Task 2 path builder. Map Scope only to the two concrete Document types. Run `validate_document("source-manifest", payload)` and `validate_document("document-manifest", payload)` before return. Sort records/aliases by bitstream ID; duplicate identity with conflicting record data raises `ManifestBuildError`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_source_manifest.py tests/test_document_identity.py tests/test_paths.py
```

Expected: all builder/identity/path tests pass.

- [ ] **Step 5: Write failing atomic-write behavior test**

Write a valid manifest below `tmp_path/root`, then mutate payload invalidly and call again. Assert first bytes remain unchanged and no `.part` sibling remains. Add escape/symlink target tests. Assert written bytes equal `stable_json_dumps(payload).encode("utf-8")` exactly.

- [ ] **Step 6: Verify RED, implement atomic validated writer, verify GREEN**

Run RED before writer exists. Implement `write_manifest` as `validate_document` followed by existing `atomic_write_text(..., allowed_root=...)`; no directory-wide delete or append path. Run:

```bash
.venv/bin/pytest -q tests/test_source_manifest.py
```

Expected: all tests pass.

- [ ] **Step 7: Document contracts and operational rollback**

`rki/Bulletins/README.md` documents canonical roots, full-ID-in-manifest rule, filename shortening, normal-Git manifests versus LFS artifacts, and no public rights implication. `Dokumentidentitaet.md` documents identity algorithms, URL/query rules, date/period source, sequence null semantics, alias relation, schema migrations, validation commands, and rollback: discard new manifest draft; never delete old document version.

- [ ] **Step 8: Run P06.1 verification matrix and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_document_identity.py tests/test_paths.py tests/test_source_manifest.py tests/test_rki_parser.py tests/test_grabber_api.py tests/test_schema_migrations.py tests/test_storage_lfs.py tests/test_storage_cli.py
.venv/bin/python scripts/validate_schemas.py
.venv/bin/python scripts/validate_p03_grabber.py
.venv/bin/python scripts/validate_p04_storage.py
.venv/bin/python scripts/validate_all_baseline.py
git diff --check
```

Expected: every command exits 0.

Commit:

```bash
git add scripts/rki_pipeline/source_manifest.py tests/test_source_manifest.py rki/Bulletins/README.md docs/Wartung/Dokumentidentitaet.md
git commit -m "feat(p06): build validated source manifests"
```

---

## Final package gate

- [ ] Run complete Python suite: `.venv/bin/pytest -q`.
- [ ] Run `python -m compileall -q scripts tests` with `.venv/bin/python`.
- [ ] Run schema, P03, P04, and baseline validators again.
- [ ] Check `git status --short`, `git diff --check`, commit list, and diff against `origin/main`.
- [ ] Obtain final whole-branch review. Fix all Critical/Important findings through reviewed fix loop.
- [ ] Only then open PR for P06.1; keep P06 overall status open until P06.2–P06.4 finish.
