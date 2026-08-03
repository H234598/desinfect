"""Fail-closed rights policy and authoritative-register contracts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.rki_pipeline import rights


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "a" * 64
POLICY_TEXT = '''schema_version = 1
default_state = "metadata_only"

[fulltext_visibility]
approved = ["public", "repository_authorized", "internal", "restricted"]
internal_only = ["internal", "restricted"]
metadata_only = []
unknown = []
takedown = []
'''


def write_policy(tmp_path: Path, text: str = POLICY_TEXT) -> Path:
    path = tmp_path / "rights-policy.toml"
    path.write_text(text, encoding="utf-8")
    return path


def write_register(tmp_path: Path, decisions: str = "") -> Path:
    path = tmp_path / "rights-register.yml"
    path.write_text(
        "schema_version: 1\ndecisions:\n" + (decisions or "  []\n"),
        encoding="utf-8",
    )
    return path


def load_test_authority(
    monkeypatch: pytest.MonkeyPatch,
    register_path: Path,
) -> rights.RightsAuthority:
    """Bind one test-scoped default source before minting authority."""

    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register_path)
    return rights.load_rights_authority()


def decision_yaml(
    state: str,
    *,
    source_id: str = SOURCE_ID,
    source_sha256: str = SOURCE_SHA256,
    basis: str = "Reviewed RKI reuse terms",
    reviewed_by: str | None = "Legal Reviewer",
    reviewed_at: str | None = "2026-08-03T08:00:00Z",
) -> str:
    reviewer = "null" if reviewed_by is None else f'"{reviewed_by}"'
    reviewed = "null" if reviewed_at is None else f'"{reviewed_at}"'
    return f'''  - source_id: "{source_id}"
    source_sha256: "{source_sha256}"
    state: "{state}"
    basis: "{basis}"
    reviewed_by: {reviewer}
    reviewed_at: {reviewed}
'''


def test_rights_policy_module_exists() -> None:
    """Catch removal of the only module allowed to resolve rights authority."""

    assert (ROOT / "scripts" / "rki_pipeline" / "rights.py").is_file()


def test_rights_policy_exposes_stable_api() -> None:
    """Catch accidental loss of the P06 rights boundary API."""

    expected = {
        "PublicationPolicy",
        "RightsAuthority",
        "RightsDecision",
        "RightsPolicy",
        "RightsPolicyError",
        "RightsRegister",
        "RightsState",
        "evaluate_rights",
        "load_rights_authority",
        "load_rights_policy",
        "load_rights_register",
        "publication_policy",
        "resolve_rights",
    }
    assert expected <= set(rights.__dict__)


@pytest.mark.parametrize(
    "override",
    (
        {"schema_version": True},
        {"default_state": "metadata_only"},
        {
            "approved_visibilities": [
                "public",
                "repository_authorized",
                "internal",
                "restricted",
            ]
        },
        {"internal_only_visibilities": ("public", "internal", "restricted")},
    ),
)
def test_rights_policy_constructor_rejects_noncanonical_fields(
    override: dict[str, object],
) -> None:
    """Direct construction must not bypass exact policy types or matrix."""

    values: dict[str, object] = {
        "schema_version": 1,
        "default_state": rights.RightsState.METADATA_ONLY,
        "approved_visibilities": (
            "public",
            "repository_authorized",
            "internal",
            "restricted",
        ),
        "internal_only_visibilities": ("internal", "restricted"),
    }
    values.update(override)

    with pytest.raises(rights.RightsPolicyError):
        rights.RightsPolicy(**values)


@pytest.mark.parametrize(
    ("override", "error"),
    (
        ({"source_id": 123}, "source_id"),
        ({"source_id": "rki:176904/12345.1"}, "source_id"),
        ({"source_sha256": "A" * 64}, "source_sha256"),
        ({"state": "approved"}, "state"),
        ({"state": rights.RightsState.UNKNOWN}, "state"),
        ({"basis": " "}, "basis"),
        ({"reviewed_by": 123}, "reviewed_by"),
        ({"reviewed_by": " Legal Reviewer "}, "reviewed_by"),
        ({"reviewed_at": "2026-08-03T08:00:00+00:00"}, "reviewed_at"),
        ({"decision_sha256": "bad"}, "decision_sha256"),
        ({"decision_sha256": "f" * 64}, "decision_sha256"),
    ),
)
def test_rights_decision_constructor_rejects_noncanonical_fields(
    override: dict[str, object],
    error: str,
) -> None:
    """Direct decisions require canonical identity, review, and recomputed hash."""

    values: dict[str, object] = {
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA256,
        "state": rights.RightsState.APPROVED,
        "basis": "Reviewed RKI reuse terms",
        "reviewed_by": "Legal Reviewer",
        "reviewed_at": "2026-08-03T08:00:00Z",
        "decision_sha256": (
            "fb219e48920e18781b8a7f8735fb8fb06bf915d4c1b276c2ea8f5e201c02d982"
        ),
    }
    values.update(override)

    with pytest.raises(rights.RightsPolicyError, match=error):
        rights.RightsDecision(**values)


def test_empty_register_defaults_exact_source_to_metadata_only(tmp_path: Path) -> None:
    """Catch any fallback that invents payload authorization from raw metadata."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register = rights.load_rights_register(write_register(tmp_path))

    decision = rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=policy,
    )

    assert decision == rights.RightsDecision(
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        state=rights.RightsState.METADATA_ONLY,
        basis="rights_register_no_match",
        reviewed_by=None,
        reviewed_at=None,
        decision_sha256=None,
    )


def test_rights_register_constructor_requires_exact_types(tmp_path: Path) -> None:
    """Direct register construction rejects bool versions and mutable entries."""

    entry = rights.load_rights_register(
        write_register(tmp_path, decision_yaml("approved"))
    ).entries[0]

    with pytest.raises(rights.RightsPolicyError):
        rights.RightsRegister(schema_version=True, entries=(entry,))
    with pytest.raises(rights.RightsPolicyError):
        rights.RightsRegister(schema_version=1, entries=[entry])
    with pytest.raises(rights.RightsPolicyError):
        rights.RightsRegister(schema_version=1, entries=("invalid",))


def test_rights_register_constructor_rejects_duplicate_or_unsorted_entries(
    tmp_path: Path,
) -> None:
    """Register lookup order is canonical and every source tuple is unique."""

    entries = rights.load_rights_register(
        write_register(
            tmp_path,
            decision_yaml("approved", source_id="rki:176904/12345")
            + decision_yaml("approved", source_id="rki:176904/12346"),
        )
    ).entries

    with pytest.raises(rights.RightsPolicyError, match="doppelt"):
        rights.RightsRegister(schema_version=1, entries=(entries[0], entries[0]))
    with pytest.raises(rights.RightsPolicyError, match="sortiert"):
        rights.RightsRegister(schema_version=1, entries=tuple(reversed(entries)))


def test_rights_authority_constructor_is_private(tmp_path: Path) -> None:
    """Only the loader may mint a publication authority capability."""

    register_path = write_register(tmp_path)

    with pytest.raises(rights.RightsPolicyError, match="Fabrik"):
        rights.RightsAuthority(register_path)


def test_rights_authority_loader_rejects_public_path_argument(tmp_path: Path) -> None:
    """Production callers cannot redirect authority to an arbitrary register."""

    with pytest.raises(TypeError):
        rights.load_rights_authority(write_register(tmp_path))


def test_loaded_authority_reloads_register_for_every_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed decision removed on disk must stop authorizing immediately."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register_path = write_register(tmp_path, decision_yaml("approved"))
    authority = load_test_authority(monkeypatch, register_path)

    allowed = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility="public",
        policy=policy,
    )
    assert allowed.payload_allowed is True

    write_register(tmp_path)
    denied = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility="public",
        policy=policy,
    )
    assert denied.payload_allowed is False


def test_loaded_authority_rejects_default_register_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability minted for another source cannot survive default-path drift."""

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_path = write_register(first_root, decision_yaml("approved"))
    second_path = write_register(second_root)
    policy = rights.load_rights_policy(write_policy(tmp_path))
    authority = load_test_authority(monkeypatch, first_path)
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", second_path)

    with pytest.raises(rights.RightsPolicyError, match="Source"):
        rights.publication_policy(
            SOURCE_ID,
            SOURCE_SHA256,
            authority=authority,
            visibility="public",
            policy=policy,
        )


def test_lookup_requires_exact_source_id_and_sha256(tmp_path: Path) -> None:
    """Catch source-only or hash-only authorization wildcards."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register = rights.load_rights_register(
        write_register(tmp_path, decision_yaml("approved"))
    )

    assert rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=policy,
    ).state is rights.RightsState.APPROVED
    assert rights.evaluate_rights(
        "rki:176904/12346",
        SOURCE_SHA256,
        register=register,
        policy=policy,
    ).state is rights.RightsState.METADATA_ONLY
    assert rights.evaluate_rights(
        SOURCE_ID,
        "b" * 64,
        register=register,
        policy=policy,
    ).state is rights.RightsState.METADATA_ONLY


def test_explicit_unknown_is_effective_metadata_only(tmp_path: Path) -> None:
    """Catch accidental payload authorization of the explicit unknown state."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register = rights.load_rights_register(
        write_register(
            tmp_path,
            decision_yaml(
                "unknown",
                basis="No reviewed publication permission",
                reviewed_by=None,
                reviewed_at=None,
            ),
        )
    )

    decision = rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=policy,
    )

    assert decision.state is rights.RightsState.METADATA_ONLY
    assert decision.decision_sha256 is not None


def test_reviewed_decision_hash_is_canonical_and_source_bound(tmp_path: Path) -> None:
    """Catch omission of identity, bytes, policy, or provenance from decision hashing."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register = rights.load_rights_register(
        write_register(tmp_path, decision_yaml("approved"))
    )

    decision = rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=policy,
    )

    assert decision.decision_sha256 == (
        "fb219e48920e18781b8a7f8735fb8fb06bf915d4c1b276c2ea8f5e201c02d982"
    )


@pytest.mark.parametrize("state", ("approved", "internal_only", "takedown"))
@pytest.mark.parametrize("missing", ("basis", "reviewed_by", "reviewed_at"))
def test_sensitive_states_require_complete_review_provenance(
    tmp_path: Path,
    state: str,
    missing: str,
) -> None:
    """Catch authorization-sensitive entries with incomplete human review."""

    values = {
        "basis": "Reviewed RKI reuse terms",
        "reviewed_by": "Legal Reviewer",
        "reviewed_at": "2026-08-03T08:00:00Z",
    }
    values[missing] = None
    entry = decision_yaml(
        state,
        basis=values["basis"] or "",
        reviewed_by=values["reviewed_by"],
        reviewed_at=values["reviewed_at"],
    )

    with pytest.raises(rights.RightsPolicyError, match=missing):
        rights.load_rights_register(write_register(tmp_path, entry))


@pytest.mark.parametrize(
    ("state", "visibility", "payload_allowed"),
    (
        ("approved", "public", True),
        ("approved", "repository_authorized", True),
        ("approved", "internal", True),
        ("approved", "restricted", True),
        ("internal_only", "public", False),
        ("internal_only", "repository_authorized", False),
        ("internal_only", "internal", True),
        ("internal_only", "restricted", True),
        ("metadata_only", "internal", False),
        ("takedown", "restricted", False),
    ),
)
def test_publication_policy_uses_fixed_visibility_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    visibility: str,
    payload_allowed: bool,
) -> None:
    """Catch public/internal visibility escalation or PDF/fulltext/ZIP bypass."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    reviewer = None if state == "metadata_only" else "Legal Reviewer"
    reviewed_at = None if state == "metadata_only" else "2026-08-03T08:00:00Z"
    register_path = write_register(
        tmp_path,
        decision_yaml(
            state,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        ),
    )
    authority = load_test_authority(
        monkeypatch,
        register_path,
    )
    result = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility=visibility,
        policy=policy,
    )

    assert result.payload_allowed is payload_allowed
    assert result.artifact_reference_allowed is payload_allowed
    assert result.metadata_allowed is True
    assert result.origin_link_allowed is True


def test_takedown_filters_mirror_references_but_keeps_origin_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch stale mirrored download references after a takedown decision."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    authority = load_test_authority(
        monkeypatch,
        write_register(tmp_path, decision_yaml("takedown")),
    )
    result = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility="public",
        policy=policy,
    )

    assert result == rights.PublicationPolicy(
        payload_allowed=False,
        artifact_reference_allowed=False,
        metadata_allowed=True,
        origin_link_allowed=True,
    )


def test_publication_policy_rejects_forged_visibility_matrix(tmp_path: Path) -> None:
    """Catch direct construction that broadens internal-only payload visibility."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    with pytest.raises(rights.RightsPolicyError, match="Matrix"):
        rights.RightsPolicy(
            schema_version=policy.schema_version,
            default_state=policy.default_state,
            approved_visibilities=policy.approved_visibilities,
            internal_only_visibilities=("public", "internal", "restricted"),
        )


def test_forged_constructed_register_cannot_authorize_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fully canonical raw register data still lacks publication authority."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    decision = rights.RightsDecision(
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        state=rights.RightsState.APPROVED,
        basis="Reviewed RKI reuse terms",
        reviewed_by="Legal Reviewer",
        reviewed_at="2026-08-03T08:00:00Z",
        decision_sha256=(
            "fb219e48920e18781b8a7f8735fb8fb06bf915d4c1b276c2ea8f5e201c02d982"
        ),
    )
    constructed_register = rights.RightsRegister(
        schema_version=1,
        entries=(decision,),
    )
    assert rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=constructed_register,
        policy=policy,
    ).state is rights.RightsState.APPROVED

    with pytest.raises(rights.RightsPolicyError, match="Authority"):
        rights.publication_policy(
            SOURCE_ID,
            SOURCE_SHA256,
            authority=constructed_register,
            visibility="public",
            policy=policy,
        )

    authority = load_test_authority(monkeypatch, write_register(tmp_path))
    result = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility="public",
        policy=policy,
    )
    assert result.payload_allowed is False
    assert result.artifact_reference_allowed is False


@pytest.mark.parametrize(
    "text",
    (
        POLICY_TEXT + "unexpected = true\n",
        POLICY_TEXT.replace('default_state = "metadata_only"', 'default_state = "approved"'),
        POLICY_TEXT.replace(
            'internal_only = ["internal", "restricted"]',
            'internal_only = ["public", "internal", "restricted"]',
        ),
    ),
)
def test_rights_policy_toml_cannot_expand_fixed_matrix(tmp_path: Path, text: str) -> None:
    """Catch reviewed TOML drift that silently broadens authorization."""

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_policy(write_policy(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    (
        "schema_version: 1\nschema_version: 1\ndecisions: []\n",
        "schema_version: 1\ndecisions: &entries []\ncopy: *entries\n",
        "schema_version: 1\ndecisions: []\n---\nschema_version: 1\ndecisions: []\n",
        "schema_version: 1\ndecisions: !!python/object:builtins.list {}\n",
        "schema_version: 1\ndecisions: []\n1: invalid-key\n",
        "schema_version: 1\ndecisions: []\n? [nested, key]\n: invalid-key\n",
    ),
)
def test_rights_register_rejects_ambiguous_or_unsafe_yaml(tmp_path: Path, text: str) -> None:
    """Catch silent duplicate override, aliases, extra documents, and unsafe tags."""

    path = tmp_path / "rights-register.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_register(path)


def test_unique_key_loader_normalizes_key_that_becomes_unhashable_on_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch raw TypeError escaping while the authority YAML is constructed."""

    class UnstableKey:
        hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            if self.hash_calls > 1:
                raise TypeError("unhashable after lookup")
            return 1

    loader = rights._UniqueKeyLoader("key: value\n")
    try:
        node = loader.get_single_node()
        key_node = node.value[0][0]
        original = loader.construct_object
        unstable = UnstableKey()

        def construct_object(candidate, deep=False):
            if candidate is key_node:
                return unstable
            return original(candidate, deep=deep)

        monkeypatch.setattr(loader, "construct_object", construct_object)
        with pytest.raises(rights.RightsPolicyError, match="Schlüssel"):
            loader.construct_mapping(node)
    finally:
        loader.dispose()


def test_rights_register_read_is_bound_to_one_open_file_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch pathname replacement between metadata validation and authority read."""

    path = write_register(tmp_path)
    replacement = tmp_path / "replacement.yml"
    replacement.write_text(
        "schema_version: 1\ndecisions:\n" + decision_yaml("approved"),
        encoding="utf-8",
    )
    original_open = os.open
    replaced = False

    def replace_after_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if dir_fd is None:
            descriptor = original_open(candidate, flags, mode)
        else:
            descriptor = original_open(candidate, flags, mode, dir_fd=dir_fd)
        if Path(candidate) == path and not replaced:
            replaced = True
            replacement.replace(path)
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)

    assert rights.load_rights_register(path).entries == ()
    assert replaced is True


def test_rights_register_read_is_bounded_to_maximum_plus_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch an unbounded authority read after a stale or racing size check."""

    path = write_register(tmp_path)
    original_fdopen = os.fdopen
    read_sizes: list[int] = []

    class TrackingHandle:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size: int = -1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracking_fdopen(*args, **kwargs):
        return TrackingHandle(original_fdopen(*args, **kwargs))

    monkeypatch.setattr(os, "fdopen", tracking_fdopen)

    rights.load_rights_register(path)

    assert read_sizes == [rights.MAX_REGISTER_BYTES + 1]


@pytest.mark.parametrize("kind", ("symlink", "directory"))
def test_rights_register_read_rejects_non_regular_descriptor(
    tmp_path: Path,
    kind: str,
) -> None:
    """Keep symlinks and non-regular authority sources outside the trust boundary."""

    target = write_register(tmp_path)
    path = tmp_path / "authority-source"
    if kind == "symlink":
        path.symlink_to(target)
    else:
        path.mkdir()

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_register(path)


def test_rights_register_read_translates_invalid_utf8(
    tmp_path: Path,
) -> None:
    """Expose malformed authority bytes only as the domain error contract."""

    path = tmp_path / "invalid-utf8.yml"
    path.write_bytes(b"schema_version: 1\ndecisions: []\n\xff")

    with pytest.raises(rights.RightsPolicyError, match="lesbar"):
        rights.load_rights_register(path)


def test_register_rejects_duplicate_authority_tuple(tmp_path: Path) -> None:
    """Catch competing reviewed decisions for identical source bytes."""

    entries = decision_yaml("approved") + decision_yaml("takedown")

    with pytest.raises(rights.RightsPolicyError, match="doppelt"):
        rights.load_rights_register(write_register(tmp_path, entries))


@pytest.mark.parametrize(
    ("source_id", "source_sha256"),
    (
        ("rki:176904/12345.1", SOURCE_SHA256),
        (SOURCE_ID, "A" * 64),
        (SOURCE_ID, "a" * 63),
    ),
)
def test_register_rejects_noncanonical_authority_keys(
    tmp_path: Path,
    source_id: str,
    source_sha256: str,
) -> None:
    """Catch identities that cannot match P06 source-manifest contracts."""

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_register(
            write_register(
                tmp_path,
                decision_yaml(
                    "approved",
                    source_id=source_id,
                    source_sha256=source_sha256,
                ),
            )
        )


def test_missing_or_oversized_register_fails_instead_of_defaulting(tmp_path: Path) -> None:
    """Catch treating an absent or unreadable authority source as a valid empty register."""

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_register(tmp_path / "missing.yml")

    oversized = tmp_path / "oversized.yml"
    oversized.write_text("#" * (1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(rights.RightsPolicyError, match="groß"):
        rights.load_rights_register(oversized)


def test_repository_rights_files_validate_offline() -> None:
    """Catch drift between reviewed repository config and runtime parser."""

    completed = subprocess.run(
        [sys.executable, "scripts/validate_rights_register.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
