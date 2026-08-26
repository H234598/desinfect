"""Fail-closed rights policy and authoritative-register contracts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from dataclasses import fields, replace

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

[publication_actions]
remove_all = []
origin_link = []
source_only = []
materialized = ["cache", "extract_text", "fetch", "hash", "index_text", "ocr", "publish", "thumbnail"]
'''


def write_policy(tmp_path: Path, text: str = POLICY_TEXT) -> Path:
    path = tmp_path / "rights-policy.toml"
    path.write_text(text, encoding="utf-8")
    return path


def write_register(tmp_path: Path, decisions: str = "") -> Path:
    path = tmp_path / "rights-register.yml"
    path.write_text(
        "schema_version: 2\ndecisions:\n" + (decisions or "  []\n"),
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
    handle = source_id.removeprefix("rki:")
    canonical_url = EXACT_CANONICAL_URL if handle.endswith(".1") else (
        f"https://edoc.rki.de/bitstream/handle/{handle}/issue.pdf?sequence=2"
    )
    bitstream_id = rights.bitstream_identity(canonical_url).bitstream_id
    mode = "materialized" if state == "approved" else "origin_link"
    if state == "takedown":
        mode = "remove_all"
    actions = tuple(action.value for action in rights.RightsAction) if state == "approved" else ()
    return exact_decision_yaml(
        source_id=source_id,
        source_sha256=source_sha256,
        canonical_url=canonical_url,
        version_or_bitstream=bitstream_id,
        state=state,
        mode=mode,
        allowed_actions=tuple(sorted(actions)),
        components_state="cleared" if state == "approved" else "unknown",
        attribution=None if state == "approved" else "null",
        basis=basis,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )


def test_rights_policy_module_exists() -> None:
    """Catch removal of the only module allowed to resolve rights authority."""

    assert (ROOT / "scripts" / "rki_pipeline" / "rights.py").is_file()


def test_canonical_synthetic_fixture_never_grants_external_publish() -> None:
    """A no-rights fixture must not become an external publication approval."""

    register = rights.load_rights_register(ROOT / "research" / "rights-register.yml")
    decision = register.entries[0]

    assert "no external publication rights claim" in decision.basis
    assert rights.RightsAction.PUBLISH not in decision.allowed_actions
    assert decision.attribution is None
    with pytest.raises(rights.RightsPolicyError, match="allowed_actions"):
        rights.resolve_action(
            decision.approval_key,
            action=rights.RightsAction.PUBLISH,
            register=register,
            policy=rights.load_rights_policy(),
        )


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
        ({"approval_key": 123}, "ApprovalKey"),
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
    tmp_path: Path,
    override: dict[str, object],
    error: str,
) -> None:
    """Direct decisions require canonical identity, review, and recomputed hash."""

    original = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    values: dict[str, object] = {
        name: getattr(original, name)
        for name in original.__dataclass_fields__
    }
    values.update(override)

    with pytest.raises(rights.RightsPolicyError, match=error):
        rights.RightsDecision(**values)


def test_empty_register_defaults_exact_source_to_unknown(tmp_path: Path) -> None:
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
        approval_key=decision.approval_key,
        state=rights.RightsState.UNKNOWN,
        mode=rights.PublicationMode.ORIGIN_LINK,
        allowed_actions=(),
        components_state=rights.ComponentsState.UNKNOWN,
        attribution=None,
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
        rights.RightsRegister(schema_version=2, entries=[entry])
    with pytest.raises(rights.RightsPolicyError):
        rights.RightsRegister(schema_version=2, entries=("invalid",))


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
        rights.RightsRegister(schema_version=2, entries=(entries[0], entries[0]))
    with pytest.raises(rights.RightsPolicyError, match="sortiert"):
        rights.RightsRegister(schema_version=2, entries=tuple(reversed(entries)))


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
    assert allowed.payload_allowed is False
    assert allowed.metadata_allowed is True

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
    ).state is rights.RightsState.METADATA_ONLY
    assert rights.evaluate_rights(
        "rki:176904/12346",
        SOURCE_SHA256,
        register=register,
        policy=policy,
    ).state is rights.RightsState.UNKNOWN
    assert rights.evaluate_rights(
        SOURCE_ID,
        "b" * 64,
        register=register,
        policy=policy,
    ).state is rights.RightsState.UNKNOWN


def test_legacy_pair_apis_project_single_materialized_match_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility pair lookups never expose payload or publication authority."""

    policy = rights.load_rights_policy(write_policy(tmp_path))
    register_path = write_exact_register(tmp_path, exact_decision_yaml())
    register = rights.load_rights_register(register_path)
    authority = load_test_authority(monkeypatch, register_path)

    evaluated = rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=policy,
    )
    resolved = rights.resolve_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        policy=policy,
    )
    publication = rights.publication_policy(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authority,
        visibility="public",
        policy=policy,
    )

    for decision in (evaluated, resolved):
        assert decision.state is rights.RightsState.METADATA_ONLY
        assert decision.mode is rights.PublicationMode.SOURCE_ONLY
        assert decision.allowed_actions == ()
        assert decision.attribution is None
        assert decision.decision_sha256 is None
    assert publication.payload_allowed is False
    assert publication.artifact_reference_allowed is False
    assert publication.metadata_allowed is True

    exact = rights.resolve_action(
        exact_key(),
        action=rights.RightsAction.PUBLISH,
        register=register,
        policy=policy,
    )
    assert exact.mode is rights.PublicationMode.MATERIALIZED
    assert rights.RightsAction.PUBLISH in exact.allowed_actions


def test_legacy_pair_lookup_for_other_bitstream_never_grants_action(tmp_path: Path) -> None:
    """A pair-only caller cannot select canonical URL or bitstream authority."""

    other_url = (
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/other.pdf?sequence=3"
    )
    register = rights.load_rights_register(
        write_exact_register(
            tmp_path,
            exact_decision_yaml(
                canonical_url=other_url,
                version_or_bitstream=rights.bitstream_identity(other_url).bitstream_id,
            ),
        )
    )

    decision = rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=register,
        policy=rights.parse_rights_policy(POLICY_V2_TEXT),
    )

    assert decision.allowed_actions == ()
    assert decision.mode is rights.PublicationMode.SOURCE_ONLY
    assert decision.decision_sha256 is None


def test_explicit_unknown_stays_fail_closed(tmp_path: Path) -> None:
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

    assert decision.state is rights.RightsState.UNKNOWN
    assert decision.allowed_actions == ()
    assert decision.decision_sha256 is not None


def test_reviewed_decision_hash_is_canonical_and_source_bound(tmp_path: Path) -> None:
    """Catch omission of identity, bytes, policy, or provenance from decision hashing."""

    register = rights.load_rights_register(
        write_register(tmp_path, decision_yaml("approved"))
    )
    decision = register.entries[0]

    changed_key = replace(decision.approval_key, source_sha256="b" * 64)
    changed = replace(decision, approval_key=changed_key, decision_sha256=None)

    assert decision.decision_sha256 == rights.decision_sha256(decision)
    assert rights.decision_sha256(changed) != decision.decision_sha256


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
        ("approved", "public", False),
        ("approved", "repository_authorized", False),
        ("approved", "internal", False),
        ("approved", "restricted", False),
        ("internal_only", "public", False),
        ("internal_only", "repository_authorized", False),
        ("internal_only", "internal", False),
        ("internal_only", "restricted", False),
        ("metadata_only", "internal", False),
        ("takedown", "restricted", False),
    ),
)
def test_publication_policy_never_promotes_actions_across_visibility(
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
    assert result.metadata_allowed is (state == "approved")
    assert result.origin_link_allowed is (state != "takedown")


def test_takedown_removes_all_mirror_and_origin_references(
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
        mode=rights.PublicationMode.REMOVE_ALL,
        allowed_actions=(),
        visibility="public",
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
    decision = rights.load_rights_register(
        write_register(tmp_path, decision_yaml("approved"))
    ).entries[0]
    constructed_register = rights.RightsRegister(
        schema_version=2,
        entries=(decision,),
    )
    assert rights.evaluate_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        register=constructed_register,
        policy=policy,
    ).state is rights.RightsState.METADATA_ONLY

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
        "schema_version: 2\nschema_version: 2\ndecisions: []\n",
        "schema_version: 2\ndecisions: &entries []\ncopy: *entries\n",
        "schema_version: 2\ndecisions: []\n---\nschema_version: 2\ndecisions: []\n",
        "schema_version: 2\ndecisions: !!python/object:builtins.list {}\n",
        "schema_version: 2\ndecisions: []\n1: invalid-key\n",
        "schema_version: 2\ndecisions: []\n? [nested, key]\n: invalid-key\n",
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
        "schema_version: 2\ndecisions:\n" + decision_yaml("approved"),
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
    path.write_bytes(b"schema_version: 2\ndecisions: []\n\xff")

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


EXACT_CANONICAL_URL = (
    "https://edoc.rki.de/bitstream/handle/176904/12345.2/issue.pdf?sequence=2"
)
EXACT_BITSTREAM_ID = (
    "rki-bitstream-ca16f3bf368deddef0cc580b31c0105db58edcfd486fa689a8860cb8aea67176"
)
POLICY_V2_TEXT = POLICY_TEXT


def exact_decision_yaml(
    *,
    source_id: str = SOURCE_ID,
    source_sha256: str = SOURCE_SHA256,
    canonical_url: str = EXACT_CANONICAL_URL,
    version_or_bitstream: str = EXACT_BITSTREAM_ID,
    state: str = "approved",
    mode: str = "materialized",
    allowed_actions: tuple[str, ...] = (
        "cache",
        "extract_text",
        "fetch",
        "hash",
        "index_text",
        "ocr",
        "publish",
        "thumbnail",
    ),
    components_state: str = "cleared",
    basis: str = "Synthetic fixture; no external publication rights claim",
    attribution: str | None = None,
    reviewed_by: str | None = "Legal Reviewer",
    reviewed_at: str | None = "2026-08-03T08:00:00Z",
) -> str:
    """Return one literal reviewed v2 fixture; never production rights evidence."""

    action_lines = "\n".join(f'      - "{action}"' for action in allowed_actions)
    if attribution is None:
        attribution = f'''
      creators:
        - "Synthetic Creator"
      attribution_parties:
        - "Synthetic Rights Holder"
      copyright_notice: "Synthetic copyright notice"
      license_notice: "CC BY 4.0"
      license_url: "https://creativecommons.org/licenses/by/4.0/"
      disclaimer_notice: "Synthetic fixture only"
      origin_url: "https://edoc.rki.de/handle/{source_id.removeprefix('rki:')}"
      prior_change_history: []
      current_change_notice: "Unchanged synthetic fixture"
'''.rstrip()
    rendered_attribution = "null" if attribution == "null" else attribution
    reviewer = "null" if reviewed_by is None else f'"{reviewed_by}"'
    reviewed = "null" if reviewed_at is None else f'"{reviewed_at}"'
    return f'''  - source_id: "{source_id}"
    canonical_url: "{canonical_url}"
    version_or_bitstream: "{version_or_bitstream}"
    source_sha256: "{source_sha256}"
    state: "{state}"
    mode: "{mode}"
    allowed_actions:{chr(10) + action_lines if action_lines else " []"}
    components_state: "{components_state}"
    attribution: {rendered_attribution}
    basis: "{basis}"
    reviewed_by: {reviewer}
    reviewed_at: {reviewed}
'''


def write_exact_register(tmp_path: Path, entry: str | None = None) -> Path:
    path = tmp_path / "rights-register-v2.yml"
    path.write_text(
        "schema_version: 2\ndecisions:\n" + (entry if entry is not None else "  []\n"),
        encoding="utf-8",
    )
    return path


def exact_key() -> object:
    return rights.ApprovalKey(
        source_id=SOURCE_ID,
        canonical_url=EXACT_CANONICAL_URL,
        version_or_bitstream=EXACT_BITSTREAM_ID,
        source_sha256=SOURCE_SHA256,
    )


def decision_variant(
    decision: rights.RightsDecision,
    **changes: object,
) -> rights.RightsDecision:
    """Return a valid synthetic decision with its changed fields hash-bound."""

    changed = replace(decision, **changes, decision_sha256=None)
    object.__setattr__(changed, "decision_sha256", rights.decision_sha256(changed))
    return changed


def test_action_contract_exposes_closed_api() -> None:
    """Removing exact revision or action types must break all effect authorization."""

    expected = {
        "ApprovalKey",
        "ComponentsState",
        "PublicationMode",
        "RightsAction",
        "RightsAttribution",
        "decision_sha256",
        "is_monotone_restriction",
        "is_not_more_permissive",
        "parse_rights_policy",
        "parse_rights_register",
        "resolve_action",
        "validate_license_url",
    }
    assert expected <= set(rights.__dict__)


def test_domain_year_and_authority_labels_never_grant_publish(tmp_path: Path) -> None:
    """RKI host, historic year, and statutory prose are never approval evidence."""

    register = rights.parse_rights_register("schema_version: 2\ndecisions: []\n")
    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    decision = rights.resolve_action(
        exact_key(),
        action=rights.RightsAction.PUBLISH,
        register=register,
        policy=policy,
    )

    assert decision.mode is rights.PublicationMode.ORIGIN_LINK
    assert decision.allowed_actions == ()
    assert decision.state is rights.RightsState.UNKNOWN


def test_byte_or_bitstream_change_resets_exact_approval(tmp_path: Path) -> None:
    """Any exact-key drift must stop matching the reviewed publication decision."""

    register = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    )
    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    approved = rights.resolve_action(
        exact_key(),
        action=rights.RightsAction.PUBLISH,
        register=register,
        policy=policy,
    )
    changed_bytes = replace(exact_key(), source_sha256="b" * 64)
    changed_bitstream = rights.ApprovalKey(
        source_id=SOURCE_ID,
        canonical_url=(
            "https://edoc.rki.de/bitstream/handle/176904/12345.2/other.pdf?sequence=2"
        ),
        version_or_bitstream=(
            "rki-bitstream-62e864b6bfea1514a40d031d26ce4696e1db4e5a0736904d0d76b8cc7c4a88d7"
        ),
        source_sha256=SOURCE_SHA256,
    )

    assert approved.mode is rights.PublicationMode.MATERIALIZED
    for changed in (changed_bytes, changed_bitstream):
        assert rights.resolve_action(
            changed,
            action=rights.RightsAction.PUBLISH,
            register=register,
            policy=policy,
        ).mode is rights.PublicationMode.ORIGIN_LINK


@pytest.mark.parametrize(
    ("actions", "components", "error"),
    (
        (("cache",), "cleared", "publish"),
        (("publish",), "unknown", "components"),
        (("publish",), "blocked", "components"),
    ),
)
def test_publish_requires_action_and_cleared_components(
    tmp_path: Path,
    actions: tuple[str, ...],
    components: str,
    error: str,
) -> None:
    """Publish must fail before effect when action or component review is missing."""

    register_path = write_exact_register(
        tmp_path,
        exact_decision_yaml(
            allowed_actions=actions,
            components_state=components,
            attribution=None if "publish" in actions else "null",
        ),
    )
    if "publish" in actions:
        with pytest.raises(rights.RightsPolicyError, match=error):
            rights.load_rights_register(register_path)
        return

    register = rights.load_rights_register(register_path)
    with pytest.raises(rights.RightsPolicyError, match="publish"):
        rights.resolve_action(
            exact_key(),
            action=rights.RightsAction.PUBLISH,
            register=register,
            policy=rights.parse_rights_policy(POLICY_V2_TEXT),
        )


@pytest.mark.parametrize(
    "field",
    (
        "creators",
        "attribution_parties",
        "copyright_notice",
        "license_notice",
        "license_url",
        "disclaimer_notice",
        "origin_url",
        "prior_change_history",
        "current_change_notice",
    ),
)
def test_every_attribution_field_is_required_and_hash_bound(
    tmp_path: Path,
    field: str,
) -> None:
    """Dropping or changing any attribution dimension must invalidate authority."""

    path = write_exact_register(tmp_path, exact_decision_yaml())
    original = rights.load_rights_register(path).entries[0]
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    removed: list[str] = []
    skipping_list = False
    target_indent = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            skipping_list = field in {"creators", "attribution_parties", "prior_change_history"}
            target_indent = len(line) - len(line.lstrip())
            continue
        if skipping_list and line.strip() and len(line) - len(line.lstrip()) > target_indent:
            continue
        skipping_list = False
        removed.append(line)
    path.write_text("\n".join(removed) + "\n", encoding="utf-8")
    with pytest.raises(rights.RightsPolicyError, match=field):
        rights.load_rights_register(path)

    assert original.attribution is not None
    changed_values: dict[str, object] = {
        "creators": ("Changed Synthetic Creator",),
        "attribution_parties": ("Changed Synthetic Rights Holder",),
        "copyright_notice": "Changed synthetic copyright",
        "license_notice": "Synthetic License",
        "license_url": "https://licenses.example.test/synthetic",
        "disclaimer_notice": "Changed synthetic fixture only",
        "origin_url": "https://edoc.rki.de/handle/176904/99999",
        "prior_change_history": ("Prior synthetic change",),
        "current_change_notice": "Changed synthetic fixture",
    }
    changed_attribution = object.__new__(rights.RightsAttribution)
    for item in fields(original.attribution):
        value = changed_values[field] if item.name == field else getattr(
            original.attribution, item.name
        )
        object.__setattr__(changed_attribution, item.name, value)
    changed = object.__new__(rights.RightsDecision)
    for item in fields(original):
        value = changed_attribution if item.name == "attribution" else getattr(
            original, item.name
        )
        object.__setattr__(changed, item.name, value)
    assert rights.decision_sha256(changed) != rights.decision_sha256(original)


@pytest.mark.parametrize(
    "value",
    (
        "http://creativecommons.org/licenses/by/4.0/",
        "https://user@creativecommons.org/licenses/by/4.0/",
        "https://creativecommons.org:443/licenses/by/4.0/",
        "https://creativecommons.org/licenses/by/4.0/?token=x",
        "https://creativecommons.org/licenses/by/4.0/#terms",
        "https://creativecommons.org/licenses/by/4.0/%0a",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        "https://creativecommons.org.evil.test/licenses/by/4.0/",
    ),
)
def test_cc_by_license_url_rejects_noncanonical_or_unsafe_values(value: str) -> None:
    """Unsafe or lookalike license links must never reach public HTML."""

    with pytest.raises(rights.RightsPolicyError, match="license_url"):
        rights.validate_license_url(value, license_notice="CC BY 4.0")


def test_cc_by_license_url_accepts_only_canonical_origin() -> None:
    assert rights.validate_license_url(
        "https://creativecommons.org/licenses/by/4.0/",
        license_notice="CC BY 4.0",
    ) == "https://creativecommons.org/licenses/by/4.0/"


@pytest.mark.parametrize(
    ("mode", "actions", "state"),
    (
        ("remove_all", ("cache",), "takedown"),
        ("origin_link", ("cache",), "metadata_only"),
        ("source_only", ("cache",), "metadata_only"),
        ("materialized", ("cache",), "metadata_only"),
    ),
)
def test_register_rejects_mode_action_or_state_escalation(
    tmp_path: Path,
    mode: str,
    actions: tuple[str, ...],
    state: str,
) -> None:
    """Only approved materialized decisions may carry explicit effects."""

    with pytest.raises(rights.RightsPolicyError):
        rights.load_rights_register(
            write_exact_register(
                tmp_path,
                exact_decision_yaml(
                    mode=mode,
                    allowed_actions=actions,
                    state=state,
                    attribution="null",
                ),
            )
        )


def test_register_rejects_unsorted_duplicate_actions_and_extra_attribution(
    tmp_path: Path,
) -> None:
    """Canonical action order and closed attribution keys prevent hash ambiguity."""

    for actions in (("publish", "cache"), ("cache", "cache")):
        with pytest.raises(rights.RightsPolicyError, match="allowed_actions"):
            rights.load_rights_register(
                write_exact_register(
                    tmp_path,
                    exact_decision_yaml(allowed_actions=actions),
                )
            )
    extra = exact_decision_yaml().replace(
        'current_change_notice: "Unchanged synthetic fixture"',
        'current_change_notice: "Unchanged synthetic fixture"\n      logo_credit: "No"',
    )
    with pytest.raises(rights.RightsPolicyError, match="Attribution"):
        rights.load_rights_register(write_exact_register(tmp_path, extra))


def test_loaders_delegate_to_pure_parsers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Captured authority bytes must have one parser, never a second path read."""

    register_path = write_exact_register(tmp_path)
    policy_path = write_policy(tmp_path, POLICY_V2_TEXT)
    seen: list[tuple[str, str]] = []
    parse_register = rights.parse_rights_register
    parse_policy = rights.parse_rights_policy

    def tracked_register(text: str):
        seen.append(("register", text))
        return parse_register(text)

    def tracked_policy(text: str):
        seen.append(("policy", text))
        return parse_policy(text)

    monkeypatch.setattr(rights, "parse_rights_register", tracked_register)
    monkeypatch.setattr(rights, "parse_rights_policy", tracked_policy)
    rights.load_rights_register(register_path)
    rights.load_rights_policy(policy_path)

    assert seen == [
        ("register", register_path.read_text(encoding="utf-8")),
        ("policy", policy_path.read_text(encoding="utf-8")),
    ]


def test_restriction_relation_orders_modes_actions_components_and_attribution(
    tmp_path: Path,
) -> None:
    """A downgrade is monotone only when every public-effect dimension narrows."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    previous = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    narrower = replace(
        previous,
        mode=rights.PublicationMode.SOURCE_ONLY,
        allowed_actions=(),
        components_state=rights.ComponentsState.BLOCKED,
        attribution=None,
        decision_sha256=None,
    )
    object.__setattr__(narrower, "decision_sha256", rights.decision_sha256(narrower))

    assert rights.is_not_more_permissive(
        narrower,
        previous,
        policy=policy,
        visibility="public",
    )
    assert rights.is_monotone_restriction(
        previous,
        narrower,
        policy=policy,
        visibility="public",
    )
    assert not rights.is_monotone_restriction(
        previous,
        previous,
        policy=policy,
        visibility="public",
    )
    attribution_drift = replace(
        previous,
        attribution=replace(
            previous.attribution,
            current_change_notice="Corrected attribution",
        ),
        decision_sha256=None,
    )
    object.__setattr__(
        attribution_drift,
        "decision_sha256",
        rights.decision_sha256(attribution_drift),
    )
    assert not rights.is_not_more_permissive(
        attribution_drift,
        previous,
        policy=policy,
        visibility="public",
    )


def test_restriction_relation_mode_matrix_and_remove_all_minimum(tmp_path: Path) -> None:
    """Every publication mode is ordered and remove_all is the unique minimum."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    materialized = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    decisions = (
        decision_variant(
            materialized,
            mode=rights.PublicationMode.REMOVE_ALL,
            allowed_actions=(),
            attribution=None,
        ),
        decision_variant(
            materialized,
            mode=rights.PublicationMode.ORIGIN_LINK,
            allowed_actions=(),
            attribution=None,
        ),
        decision_variant(
            materialized,
            mode=rights.PublicationMode.SOURCE_ONLY,
            allowed_actions=(),
            attribution=None,
        ),
        materialized,
    )

    for current_index, current in enumerate(decisions):
        for previous_index, previous in enumerate(decisions):
            assert rights.is_not_more_permissive(
                current,
                previous,
                policy=policy,
                visibility="public",
            ) is (current_index <= previous_index)
    assert rights.is_not_more_permissive(
        decisions[0],
        decision_variant(
            decisions[1],
            state=rights.RightsState.TAKEDOWN,
            components_state=rights.ComponentsState.BLOCKED,
        ),
        policy=policy,
        visibility="public",
    )


def test_restriction_relation_is_reflexive_antisymmetric_and_transitive(
    tmp_path: Path,
) -> None:
    """The non-strict effect order obeys its declared partial-order laws."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    most = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    chain = (
        decision_variant(
            most,
            mode=rights.PublicationMode.REMOVE_ALL,
            state=rights.RightsState.TAKEDOWN,
            allowed_actions=(),
            components_state=rights.ComponentsState.BLOCKED,
            attribution=None,
        ),
        decision_variant(
            most,
            mode=rights.PublicationMode.ORIGIN_LINK,
            state=rights.RightsState.UNKNOWN,
            allowed_actions=(),
            components_state=rights.ComponentsState.UNKNOWN,
            attribution=None,
        ),
        decision_variant(
            most,
            mode=rights.PublicationMode.SOURCE_ONLY,
            state=rights.RightsState.METADATA_ONLY,
            allowed_actions=(),
            components_state=rights.ComponentsState.UNKNOWN,
            attribution=None,
        ),
        most,
    )

    for decision in chain:
        assert rights.is_not_more_permissive(
            decision,
            decision,
            policy=policy,
            visibility="public",
        )
        assert not rights.is_monotone_restriction(
            decision,
            decision,
            policy=policy,
            visibility="public",
        )
    for left in chain:
        for right in chain:
            left_le_right = rights.is_not_more_permissive(
                left, right, policy=policy, visibility="public"
            )
            right_le_left = rights.is_not_more_permissive(
                right, left, policy=policy, visibility="public"
            )
            if left_le_right and right_le_left:
                assert left == right
    for first in chain:
        for second in chain:
            for third in chain:
                if rights.is_not_more_permissive(
                    first, second, policy=policy, visibility="public"
                ) and rights.is_not_more_permissive(
                    second, third, policy=policy, visibility="public"
                ):
                    assert rights.is_not_more_permissive(
                        first, third, policy=policy, visibility="public"
                    )


def test_restriction_relation_covers_states_components_and_action_sets(
    tmp_path: Path,
) -> None:
    """Every declared state/component and representative action set is ordered."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    approved = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    state_chain = tuple(
        decision_variant(
            approved,
            mode=rights.PublicationMode.ORIGIN_LINK,
            state=state,
            allowed_actions=(),
            components_state=rights.ComponentsState.UNKNOWN,
            attribution=None,
        )
        for state in (
            rights.RightsState.TAKEDOWN,
            rights.RightsState.UNKNOWN,
            rights.RightsState.METADATA_ONLY,
            rights.RightsState.INTERNAL_ONLY,
            rights.RightsState.APPROVED,
        )
    )
    component_chain = tuple(
        decision_variant(
            approved,
            allowed_actions=(rights.RightsAction.CACHE,),
            components_state=component,
            attribution=None,
        )
        for component in (
            rights.ComponentsState.BLOCKED,
            rights.ComponentsState.UNKNOWN,
            rights.ComponentsState.CLEARED,
        )
    )
    action_chain = tuple(
        decision_variant(approved, allowed_actions=actions, attribution=None)
        for actions in (
            (),
            (rights.RightsAction.CACHE,),
            (rights.RightsAction.CACHE, rights.RightsAction.FETCH),
        )
    )

    for chain in (state_chain, component_chain, action_chain):
        for current_index, current in enumerate(chain):
            for previous_index, previous in enumerate(chain):
                assert rights.is_not_more_permissive(
                    current,
                    previous,
                    policy=policy,
                    visibility="public",
                ) is (current_index <= previous_index)


def test_nonpublic_visibility_has_no_public_download_effect(tmp_path: Path) -> None:
    """A nonpublic reference is a strict public-effect restriction by itself."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    decision = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]

    assert rights.is_not_more_permissive(
        decision,
        decision,
        policy=policy,
        visibility="internal",
    )
    assert rights.is_monotone_restriction(
        decision,
        decision,
        policy=policy,
        visibility="internal",
    )


def test_restriction_rejects_rewritten_attribution_history(tmp_path: Path) -> None:
    """Attribution history may only retain its prior prefix before quarantine."""

    policy = rights.parse_rights_policy(POLICY_V2_TEXT)
    previous = rights.load_rights_register(
        write_exact_register(tmp_path, exact_decision_yaml())
    ).entries[0]
    assert previous.attribution is not None
    appended = decision_variant(
        previous,
        attribution=replace(
            previous.attribution,
            prior_change_history=("Synthetic prior correction",),
            current_change_notice="Synthetic current correction",
        ),
    )
    assert not rights.is_not_more_permissive(
        appended,
        previous,
        policy=policy,
        visibility="public",
    )
    rewritten = decision_variant(
        appended,
        attribution=replace(
            appended.attribution,
            prior_change_history=("Rewritten history",),
        ),
    )
    with pytest.raises(rights.RightsPolicyError, match="append-only"):
        rights.is_not_more_permissive(
            rewritten,
            appended,
            policy=policy,
            visibility="public",
        )
