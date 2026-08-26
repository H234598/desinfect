"""Fail-closed rights decisions for exact RKI source revisions and actions."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from scripts.rki_pipeline.documents import DocumentIdentityError, bitstream_identity
from scripts.rki_pipeline.io_utils import sha256_bytes, stable_json_dumps


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "rights-policy.toml"
DEFAULT_REGISTER_PATH = ROOT / "research" / "rights-register.yml"
MAX_POLICY_BYTES = 64 * 1024
MAX_REGISTER_BYTES = 1024 * 1024
_SOURCE_ID = re.compile(r"^rki:176904/[0-9]+(?:\.(?:[2-9]|[1-9][0-9]+))?$")
_BITSTREAM_ID = re.compile(r"^rki-bitstream-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVIEWED_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_VISIBILITIES = ("public", "repository_authorized", "internal", "restricted")
_APPROVED_VISIBILITIES = _VISIBILITIES
_INTERNAL_VISIBILITIES = ("internal", "restricted")
_AUTHORITY_TOKEN = object()


class RightsPolicyError(ValueError):
    """Rights configuration or an authority lookup is unsafe or malformed."""


class RightsState(StrEnum):
    """Closed set of reviewed and fail-closed rights states."""

    APPROVED = "approved"
    METADATA_ONLY = "metadata_only"
    INTERNAL_ONLY = "internal_only"
    UNKNOWN = "unknown"
    TAKEDOWN = "takedown"


class RightsAction(StrEnum):
    """Closed set of independently reviewed payload/publication effects."""

    FETCH = "fetch"
    CACHE = "cache"
    HASH = "hash"
    OCR = "ocr"
    EXTRACT_TEXT = "extract_text"
    THUMBNAIL = "thumbnail"
    INDEX_TEXT = "index_text"
    PUBLISH = "publish"


class PublicationMode(StrEnum):
    """Closed public projections ordered from least to most permissive."""

    REMOVE_ALL = "remove_all"
    ORIGIN_LINK = "origin_link"
    SOURCE_ONLY = "source_only"
    MATERIALIZED = "materialized"


class ComponentsState(StrEnum):
    """Independent review state for third-party components."""

    CLEARED = "cleared"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


_SENSITIVE_STATES = frozenset(
    {RightsState.APPROVED, RightsState.INTERNAL_ONLY, RightsState.TAKEDOWN}
)
_ALL_ACTIONS = tuple(sorted(RightsAction, key=lambda item: item.value))
_PUBLICATION_ACTIONS = (
    (PublicationMode.REMOVE_ALL, ()),
    (PublicationMode.ORIGIN_LINK, ()),
    (PublicationMode.SOURCE_ONLY, ()),
    (PublicationMode.MATERIALIZED, _ALL_ACTIONS),
)
_ENTRY_KEYS = frozenset(
    {
        "source_id",
        "canonical_url",
        "version_or_bitstream",
        "source_sha256",
        "state",
        "mode",
        "allowed_actions",
        "components_state",
        "attribution",
        "basis",
        "reviewed_by",
        "reviewed_at",
    }
)
_ATTRIBUTION_KEYS = frozenset(
    {
        "creators",
        "attribution_parties",
        "copyright_notice",
        "license_notice",
        "license_url",
        "disclaimer_notice",
        "origin_url",
        "prior_change_history",
        "current_change_notice",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalKey:
    """Exact source identity, canonical bitstream, and immutable source bytes."""

    source_id: str
    canonical_url: str
    version_or_bitstream: str
    source_sha256: str

    def __post_init__(self) -> None:
        _validate_approval_key(self)


@dataclass(frozen=True, slots=True)
class RightsAttribution:
    """Complete attribution evidence required by a reviewed publish action."""

    creators: tuple[str, ...]
    attribution_parties: tuple[str, ...]
    copyright_notice: str
    license_notice: str
    license_url: str
    disclaimer_notice: str
    origin_url: str
    prior_change_history: tuple[str, ...]
    current_change_notice: str

    def __post_init__(self) -> None:
        _validate_attribution(self)


@dataclass(frozen=True, slots=True)
class RightsPolicy:
    """Immutable publication and action matrix loaded from reviewed TOML."""

    schema_version: int
    default_state: RightsState
    approved_visibilities: tuple[str, ...]
    internal_only_visibilities: tuple[str, ...]
    publication_actions: tuple[
        tuple[PublicationMode, tuple[RightsAction, ...]], ...
    ] = _PUBLICATION_ACTIONS

    def __post_init__(self) -> None:
        _validate_policy_instance(self)

    def actions_for_mode(self, mode: PublicationMode) -> tuple[RightsAction, ...]:
        """Return fixed action ceiling for one exact mode."""

        if type(mode) is not PublicationMode:
            raise RightsPolicyError("mode ist kein kanonischer PublicationMode")
        return dict(self.publication_actions)[mode]


@dataclass(frozen=True, slots=True)
class RightsDecision:
    """One reviewed decision bound to an exact approval key and effects."""

    approval_key: ApprovalKey
    state: RightsState
    mode: PublicationMode
    allowed_actions: tuple[RightsAction, ...]
    components_state: ComponentsState
    attribution: RightsAttribution | None
    basis: str
    reviewed_by: str | None
    reviewed_at: str | None
    decision_sha256: str | None

    def __post_init__(self) -> None:
        _validate_decision_instance(self, require_hash=False)

    @property
    def source_id(self) -> str:
        return self.approval_key.source_id

    @property
    def source_sha256(self) -> str:
        return self.approval_key.source_sha256


@dataclass(frozen=True, slots=True)
class RightsRegister:
    """Immutable reviewed register entries."""

    schema_version: int
    entries: tuple[RightsDecision, ...]

    def __post_init__(self) -> None:
        _validate_register_instance(self)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class RightsAuthority:
    """Opaque capability minted only from a validated register source."""

    _register_source: Path
    _token: object
    _isolated: bool

    def __init__(
        self,
        register_source: Path,
        *,
        _token: object | None = None,
        _isolated: bool = False,
    ) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise RightsPolicyError(
                "RightsAuthority kann nur durch die Loader-Fabrik erzeugt werden"
            )
        object.__setattr__(self, "_register_source", register_source)
        object.__setattr__(self, "_token", _token)
        object.__setattr__(self, "_isolated", _isolated)


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    """Derived compatibility view; actions and mode remain sole truth."""

    mode: PublicationMode
    allowed_actions: tuple[RightsAction, ...]
    visibility: str

    def __post_init__(self) -> None:
        if type(self.mode) is not PublicationMode:
            raise RightsPolicyError("PublicationPolicy.mode ist nicht kanonisch")
        _validate_actions(self.allowed_actions)
        if self.visibility not in _VISIBILITIES:
            raise RightsPolicyError("PublicationPolicy.visibility ist unbekannt")

    def action_allowed(self, action: RightsAction) -> bool:
        """Return whether exact action is explicitly present."""

        if type(action) is not RightsAction:
            raise RightsPolicyError("action ist keine kanonische RightsAction")
        return action in self.allowed_actions

    @property
    def payload_allowed(self) -> bool:
        return bool(self.allowed_actions)

    @property
    def artifact_reference_allowed(self) -> bool:
        return self.action_allowed(RightsAction.PUBLISH)

    @property
    def metadata_allowed(self) -> bool:
        return self.mode in {PublicationMode.SOURCE_ONLY, PublicationMode.MATERIALIZED}

    @property
    def origin_link_allowed(self) -> bool:
        return self.mode is not PublicationMode.REMOVE_ALL


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects silent duplicate mapping keys."""

    def construct_mapping(
        self,
        node: yaml.nodes.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        result: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise RightsPolicyError("YAML-Schlüssel muss skalar sein") from exc
            if duplicate:
                raise RightsPolicyError(f"Doppelter YAML-Schlüssel: {key!r}")
            value = self.construct_object(value_node, deep=deep)
            try:
                result[key] = value
            except TypeError as exc:
                raise RightsPolicyError("YAML-Schlüssel muss skalar sein") from exc
        return result


def _read_text(path: Path, *, maximum: int, label: str) -> str:
    path = Path(path)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RightsPolicyError(f"{label} kann Symlinks nicht sicher ausschließen")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RightsPolicyError(f"{label} ist keine reguläre Datei: {path}")
        if metadata.st_size > maximum:
            raise RightsPolicyError(f"{label} ist zu groß: {path}")
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = None
        with handle:
            payload = handle.read(maximum + 1)
        if len(payload) > maximum:
            raise RightsPolicyError(f"{label} ist zu groß: {path}")
        return payload.decode("utf-8")
    except RightsPolicyError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RightsPolicyError(f"{label} ist nicht lesbar: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _exact_keys(value: Mapping[object, object], expected: frozenset[str], *, label: str) -> None:
    keys = set(value)
    if keys != expected:
        extra = sorted(repr(key) for key in keys - expected)
        raise RightsPolicyError(
            f"{label} besitzt falsche Schlüssel; missing={sorted(expected - keys)}; "
            f"extra={extra}"
        )


def _required_string(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise RightsPolicyError(f"{name} muss eine nichtleere Zeichenkette sein")
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise RightsPolicyError(
            f"{name} muss kanonisch ohne Steuer- oder Randzeichen sein"
        )
    return value


def _optional_string(value: object, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_string(value, name, maximum=maximum)


def _string_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool,
    maximum: int = 1000,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise RightsPolicyError(f"{name} muss eine Stringliste sein")
    result = tuple(_required_string(item, name, maximum=maximum) for item in value)
    if not allow_empty and not result:
        raise RightsPolicyError(f"{name} darf nicht leer sein")
    if len(set(result)) != len(result):
        raise RightsPolicyError(f"{name} muss eindeutig sein")
    return result


def _reviewed_at(value: object) -> str | None:
    reviewed_at = _optional_string(value, "reviewed_at", maximum=40)
    if reviewed_at is None:
        return None
    if _REVIEWED_AT.fullmatch(reviewed_at) is None:
        raise RightsPolicyError("reviewed_at muss ein kanonischer UTC-Zeitpunkt sein")
    try:
        parsed = datetime.fromisoformat(reviewed_at[:-1] + "+00:00")
    except ValueError as exc:
        raise RightsPolicyError("reviewed_at ist kein gültiger UTC-Zeitpunkt") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise RightsPolicyError("reviewed_at muss UTC sein")
    return reviewed_at


def _safe_https_url(value: object, *, name: str, maximum: int = 2000) -> str:
    url = _required_string(value, name, maximum=maximum)
    if (
        not url.isascii()
        or "\\" in url
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", url, re.I)
    ):
        raise RightsPolicyError(f"{name} enthält verschleierte oder unsichere Zeichen")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RightsPolicyError(f"{name} besitzt einen ungültigen Port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.hostname
    ):
        raise RightsPolicyError(f"{name} muss eine kanonische absolute HTTPS-URL sein")
    return url


def validate_license_url(value: str, *, license_notice: str) -> str:
    """Validate license links independently from origin and artifact URLs."""

    notice = _required_string(license_notice, "license_notice", maximum=200)
    url = _safe_https_url(value, name="license_url")
    if notice == "CC BY 4.0" and url != "https://creativecommons.org/licenses/by/4.0/":
        raise RightsPolicyError("license_url ist für CC BY 4.0 nicht kanonisch")
    return url


def _validate_origin_url(value: object, *, source_id: str) -> str:
    url = _safe_https_url(value, name="origin_url")
    handle = source_id.removeprefix("rki:")
    if url != f"https://edoc.rki.de/handle/{handle}":
        raise RightsPolicyError("origin_url passt nicht zur kanonischen source_id")
    return url


def _validate_approval_key(key: ApprovalKey) -> None:
    if type(key) is not ApprovalKey:
        raise RightsPolicyError("ApprovalKey besitzt keinen exakten Typ")
    if type(key.source_id) is not str or _SOURCE_ID.fullmatch(key.source_id) is None:
        raise RightsPolicyError("source_id ist keine kanonische RKI-Quell-ID")
    if (
        type(key.source_sha256) is not str
        or _SHA256.fullmatch(key.source_sha256) is None
    ):
        raise RightsPolicyError("source_sha256 muss ein kleingeschriebener SHA-256 sein")
    if type(key.canonical_url) is not str:
        raise RightsPolicyError("canonical_url muss eine Zeichenkette sein")
    try:
        identity = bitstream_identity(key.canonical_url)
    except DocumentIdentityError as exc:
        raise RightsPolicyError(
            "canonical_url ist keine kanonische RKI-Bitstream-URL"
        ) from exc
    if identity.canonical_url != key.canonical_url:
        raise RightsPolicyError("canonical_url ist nicht kanonisch")
    handle = key.source_id.removeprefix("rki:")
    if not urlsplit(key.canonical_url).path.startswith(
        f"/bitstream/handle/{handle}/"
    ):
        raise RightsPolicyError("canonical_url passt nicht zur source_id")
    if (
        type(key.version_or_bitstream) is not str
        or _BITSTREAM_ID.fullmatch(key.version_or_bitstream) is None
        or key.version_or_bitstream != identity.bitstream_id
    ):
        raise RightsPolicyError("version_or_bitstream passt nicht zur canonical_url")


def _validate_attribution(attribution: RightsAttribution) -> None:
    if type(attribution) is not RightsAttribution:
        raise RightsPolicyError("Attribution besitzt keinen exakten Typ")
    for name in ("creators", "attribution_parties", "prior_change_history"):
        value = getattr(attribution, name)
        if type(value) is not tuple or any(type(item) is not str for item in value):
            raise RightsPolicyError(f"{name} muss ein unveränderliches Stringtupel sein")
        if name != "prior_change_history" and not value:
            raise RightsPolicyError(f"{name} darf nicht leer sein")
        if len(set(value)) != len(value):
            raise RightsPolicyError(f"{name} muss eindeutig sein")
        for item in value:
            _required_string(item, name, maximum=1000)
    for name in (
        "copyright_notice",
        "license_notice",
        "disclaimer_notice",
        "current_change_notice",
    ):
        _required_string(getattr(attribution, name), name, maximum=2000)
    validate_license_url(
        attribution.license_url,
        license_notice=attribution.license_notice,
    )


def _validate_actions(value: tuple[RightsAction, ...]) -> None:
    if type(value) is not tuple or any(
        type(action) is not RightsAction for action in value
    ):
        raise RightsPolicyError("allowed_actions muss ein exaktes RightsAction-Tupel sein")
    if value != tuple(sorted(set(value), key=lambda action: action.value)):
        raise RightsPolicyError("allowed_actions muss sortiert und eindeutig sein")


def _validate_decision_instance(
    decision: RightsDecision,
    *,
    require_hash: bool,
) -> None:
    if type(decision) is not RightsDecision:
        raise RightsPolicyError("RightsDecision besitzt keinen exakten Typ")
    _validate_approval_key(decision.approval_key)
    if type(decision.state) is not RightsState:
        raise RightsPolicyError("state ist kein kanonischer Rechtezustand")
    if type(decision.mode) is not PublicationMode:
        raise RightsPolicyError("mode ist kein kanonischer Publikationsmodus")
    _validate_actions(decision.allowed_actions)
    if type(decision.components_state) is not ComponentsState:
        raise RightsPolicyError("components_state ist nicht kanonisch")
    if decision.attribution is not None:
        _validate_attribution(decision.attribution)
        expected_origin = (
            "https://edoc.rki.de/handle/" + decision.source_id.removeprefix("rki:")
        )
        if decision.attribution.origin_url != expected_origin:
            raise RightsPolicyError("origin_url passt nicht zur source_id")
    basis = _required_string(decision.basis, "basis", maximum=1000)
    reviewed_by = _optional_string(decision.reviewed_by, "reviewed_by", maximum=200)
    reviewed_at = _reviewed_at(decision.reviewed_at)

    synthetic_missing = (
        decision.state is RightsState.UNKNOWN
        and decision.mode is PublicationMode.ORIGIN_LINK
        and decision.allowed_actions == ()
        and decision.components_state is ComponentsState.UNKNOWN
        and decision.attribution is None
        and basis == "rights_register_no_match"
        and reviewed_by is None
        and reviewed_at is None
        and decision.decision_sha256 is None
    )
    if synthetic_missing:
        return
    if basis == "rights_register_no_match":
        raise RightsPolicyError("basis rights_register_no_match ist synthetisch reserviert")
    if (reviewed_by is None) != (reviewed_at is None):
        raise RightsPolicyError("reviewed_by und reviewed_at müssen gemeinsam gesetzt sein")
    if decision.state in _SENSITIVE_STATES and reviewed_by is None:
        raise RightsPolicyError(f"reviewed_by fehlt für Zustand {decision.state.value}")
    if decision.state in _SENSITIVE_STATES and reviewed_at is None:
        raise RightsPolicyError(f"reviewed_at fehlt für Zustand {decision.state.value}")

    if decision.mode is not PublicationMode.MATERIALIZED:
        if decision.allowed_actions:
            raise RightsPolicyError(
                f"mode {decision.mode.value} erlaubt keine allowed_actions"
            )
        if decision.attribution is not None:
            raise RightsPolicyError(
                f"mode {decision.mode.value} erlaubt keine Attribution"
            )
    elif decision.state is not RightsState.APPROVED:
        raise RightsPolicyError("materialized erzwingt state approved")
    if RightsAction.PUBLISH in decision.allowed_actions and decision.attribution is None:
        raise RightsPolicyError("publish erfordert vollständige Attribution")
    if (
        RightsAction.PUBLISH in decision.allowed_actions
        and decision.components_state is not ComponentsState.CLEARED
    ):
        raise RightsPolicyError("components_state muss für publish cleared sein")
    if RightsAction.PUBLISH not in decision.allowed_actions and decision.attribution is not None:
        raise RightsPolicyError("Attribution ohne publish-Aktion ist unzulässig")

    if decision.decision_sha256 is None:
        if require_hash:
            raise RightsPolicyError("decision_sha256 fehlt")
        return
    if (
        type(decision.decision_sha256) is not str
        or _SHA256.fullmatch(decision.decision_sha256) is None
    ):
        raise RightsPolicyError(
            "decision_sha256 muss ein kleingeschriebener SHA-256 sein"
        )
    if decision.decision_sha256 != decision_sha256(decision):
        raise RightsPolicyError(
            "decision_sha256 stimmt nicht mit der Entscheidung überein"
        )


def _attribution_payload(attribution: RightsAttribution | None) -> object:
    if attribution is None:
        return None
    return {
        "creators": list(attribution.creators),
        "attribution_parties": list(attribution.attribution_parties),
        "copyright_notice": attribution.copyright_notice,
        "license_notice": attribution.license_notice,
        "license_url": attribution.license_url,
        "disclaimer_notice": attribution.disclaimer_notice,
        "origin_url": attribution.origin_url,
        "prior_change_history": list(attribution.prior_change_history),
        "current_change_notice": attribution.current_change_notice,
    }


def decision_sha256(decision: RightsDecision) -> str:
    """Hash exact identity, effect, attribution, and review dimensions."""

    if type(decision) is not RightsDecision:
        raise RightsPolicyError("decision muss ein exakter RightsDecision sein")
    key = decision.approval_key
    payload = {
        "policy_version": 1,
        "approval_key": {
            "source_id": key.source_id,
            "canonical_url": key.canonical_url,
            "version_or_bitstream": key.version_or_bitstream,
            "source_sha256": key.source_sha256,
        },
        "state": decision.state.value,
        "mode": decision.mode.value,
        "allowed_actions": [action.value for action in decision.allowed_actions],
        "components_state": decision.components_state.value,
        "attribution": _attribution_payload(decision.attribution),
        "basis": decision.basis,
        "reviewed_by": decision.reviewed_by,
        "reviewed_at": decision.reviewed_at,
    }
    return sha256_bytes(stable_json_dumps(payload).encode("utf-8"))


def _policy_matrix(data: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(data, dict):
        raise RightsPolicyError("[fulltext_visibility] muss eine TOML-Tabelle sein")
    expected = frozenset(state.value for state in RightsState)
    _exact_keys(data, expected, label="fulltext_visibility")
    for state in expected:
        if not isinstance(data[state], list) or not all(
            type(value) is str for value in data[state]
        ):
            raise RightsPolicyError(
                f"fulltext_visibility.{state} muss Stringliste sein"
            )
    values = {state: tuple(data[state]) for state in expected}
    required = {
        RightsState.APPROVED.value: _APPROVED_VISIBILITIES,
        RightsState.INTERNAL_ONLY.value: _INTERNAL_VISIBILITIES,
        RightsState.METADATA_ONLY.value: (),
        RightsState.UNKNOWN.value: (),
        RightsState.TAKEDOWN.value: (),
    }
    if values != required:
        raise RightsPolicyError("Volltext-Sichtbarkeitsmatrix darf nicht verändert werden")
    return values[RightsState.APPROVED], values[RightsState.INTERNAL_ONLY]


def _action_matrix(
    data: object,
) -> tuple[tuple[PublicationMode, tuple[RightsAction, ...]], ...]:
    if not isinstance(data, dict):
        raise RightsPolicyError("[publication_actions] muss eine TOML-Tabelle sein")
    expected = frozenset(mode.value for mode in PublicationMode)
    _exact_keys(data, expected, label="publication_actions")
    matrix: list[tuple[PublicationMode, tuple[RightsAction, ...]]] = []
    for mode in PublicationMode:
        raw = data[mode.value]
        if not isinstance(raw, list):
            raise RightsPolicyError(
                f"publication_actions.{mode.value} muss Liste sein"
            )
        try:
            actions = tuple(RightsAction(value) for value in raw)
        except (TypeError, ValueError) as exc:
            raise RightsPolicyError(
                "publication_actions enthält unbekannte Aktion"
            ) from exc
        _validate_actions(actions)
        matrix.append((mode, actions))
    result = tuple(matrix)
    if result != _PUBLICATION_ACTIONS:
        raise RightsPolicyError("Publikations-Aktionsmatrix darf nicht erweitert werden")
    return result


def parse_rights_policy(text: str) -> RightsPolicy:
    """Parse captured policy bytes without a second path read."""

    if type(text) is not str:
        raise RightsPolicyError("Rechtepolicytext muss String sein")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RightsPolicyError("Rechtepolicy ist kein gültiges TOML") from exc
    _exact_keys(
        data,
        frozenset(
            {
                "schema_version",
                "default_state",
                "fulltext_visibility",
                "publication_actions",
            }
        ),
        label="Rechtepolicy",
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise RightsPolicyError("Unbekannte Rechtepolicy-Version")
    if data["default_state"] != RightsState.METADATA_ONLY.value:
        raise RightsPolicyError("default_state muss unveränderlich metadata_only sein")
    approved, internal = _policy_matrix(data["fulltext_visibility"])
    return RightsPolicy(
        schema_version=1,
        default_state=RightsState.METADATA_ONLY,
        approved_visibilities=approved,
        internal_only_visibilities=internal,
        publication_actions=_action_matrix(data["publication_actions"]),
    )


def load_rights_policy(path: Path = DEFAULT_POLICY_PATH) -> RightsPolicy:
    """Load reviewed rights rules through pure parser."""

    return parse_rights_policy(
        _read_text(path, maximum=MAX_POLICY_BYTES, label="Rechtepolicy")
    )


def _yaml_document(text: str) -> dict[str, Any]:
    try:
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise RightsPolicyError("YAML-Anker und -Aliase sind unzulässig")
        documents = list(yaml.load_all(text, Loader=_UniqueKeyLoader))
    except RightsPolicyError:
        raise
    except yaml.YAMLError as exc:
        raise RightsPolicyError(
            "Rights-Register ist kein eindeutiges Safe-YAML"
        ) from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise RightsPolicyError("Rights-Register muss exakt ein YAML-Objekt enthalten")
    return documents[0]


def _parse_attribution(value: object, *, source_id: str) -> RightsAttribution | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RightsPolicyError("Attribution muss Objekt oder null sein")
    _exact_keys(value, _ATTRIBUTION_KEYS, label="Attribution")
    return RightsAttribution(
        creators=_string_tuple(value["creators"], "creators", allow_empty=False),
        attribution_parties=_string_tuple(
            value["attribution_parties"],
            "attribution_parties",
            allow_empty=False,
        ),
        copyright_notice=_required_string(
            value["copyright_notice"], "copyright_notice", maximum=2000
        ),
        license_notice=_required_string(
            value["license_notice"], "license_notice", maximum=200
        ),
        license_url=validate_license_url(
            value["license_url"], license_notice=value["license_notice"]
        ),
        disclaimer_notice=_required_string(
            value["disclaimer_notice"], "disclaimer_notice", maximum=2000
        ),
        origin_url=_validate_origin_url(value["origin_url"], source_id=source_id),
        prior_change_history=_string_tuple(
            value["prior_change_history"],
            "prior_change_history",
            allow_empty=True,
        ),
        current_change_notice=_required_string(
            value["current_change_notice"], "current_change_notice", maximum=2000
        ),
    )


def _register_decision(value: object) -> RightsDecision:
    if not isinstance(value, dict):
        raise RightsPolicyError("Jede Registerentscheidung muss ein Objekt sein")
    _exact_keys(value, _ENTRY_KEYS, label="Registerentscheidung")
    source_id = _required_string(value["source_id"], "source_id", maximum=200)
    key = ApprovalKey(
        source_id=source_id,
        canonical_url=_required_string(
            value["canonical_url"], "canonical_url", maximum=2000
        ),
        version_or_bitstream=_required_string(
            value["version_or_bitstream"], "version_or_bitstream", maximum=200
        ),
        source_sha256=_required_string(
            value["source_sha256"], "source_sha256", maximum=64
        ),
    )
    try:
        state = RightsState(value["state"])
    except (TypeError, ValueError) as exc:
        raise RightsPolicyError("state ist kein bekannter Rechtezustand") from exc
    try:
        mode = PublicationMode(value["mode"])
    except (TypeError, ValueError) as exc:
        raise RightsPolicyError("mode ist kein bekannter Publikationsmodus") from exc
    raw_actions = value["allowed_actions"]
    if not isinstance(raw_actions, list):
        raise RightsPolicyError("allowed_actions muss eine Liste sein")
    try:
        actions = tuple(RightsAction(action) for action in raw_actions)
    except (TypeError, ValueError) as exc:
        raise RightsPolicyError("allowed_actions enthält unbekannte Aktion") from exc
    _validate_actions(actions)
    try:
        components = ComponentsState(value["components_state"])
    except (TypeError, ValueError) as exc:
        raise RightsPolicyError("components_state ist unbekannt") from exc
    basis = _required_string(value["basis"], "basis", maximum=1000)
    reviewed_by = _optional_string(value["reviewed_by"], "reviewed_by", maximum=200)
    reviewed_at = _reviewed_at(value["reviewed_at"])
    draft = RightsDecision(
        approval_key=key,
        state=state,
        mode=mode,
        allowed_actions=actions,
        components_state=components,
        attribution=_parse_attribution(value["attribution"], source_id=source_id),
        basis=basis,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        decision_sha256=None,
    )
    return replace(draft, decision_sha256=decision_sha256(draft))


def parse_rights_register(text: str) -> RightsRegister:
    """Parse captured register bytes without a second path read."""

    if type(text) is not str:
        raise RightsPolicyError("Rights-Registertext muss String sein")
    data = _yaml_document(text)
    _exact_keys(
        data,
        frozenset({"schema_version", "decisions"}),
        label="Rights-Register",
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 2:
        raise RightsPolicyError("Unbekannte Rights-Register-Version")
    values = data["decisions"]
    if not isinstance(values, list):
        raise RightsPolicyError("decisions muss eine Liste sein")
    entries = tuple(
        sorted(
            (_register_decision(value) for value in values),
            key=lambda item: _approval_tuple(item.approval_key),
        )
    )
    return RightsRegister(schema_version=2, entries=entries)


def load_rights_register(path: Path = DEFAULT_REGISTER_PATH) -> RightsRegister:
    """Load reviewed source decisions through pure parser."""

    return parse_rights_register(
        _read_text(path, maximum=MAX_REGISTER_BYTES, label="Rights-Register")
    )


def _approval_tuple(key: ApprovalKey) -> tuple[str, str, str, str]:
    return (
        key.source_id,
        key.canonical_url,
        key.version_or_bitstream,
        key.source_sha256,
    )


def _validate_register_instance(register: RightsRegister) -> None:
    if type(register) is not RightsRegister:
        raise RightsPolicyError("RightsRegister besitzt keinen exakten Typ")
    if type(register.schema_version) is not int or register.schema_version != 2:
        raise RightsPolicyError("Unbekannte Rights-Register-Version")
    if type(register.entries) is not tuple:
        raise RightsPolicyError("entries muss ein unveränderliches Tupel sein")
    if any(type(entry) is not RightsDecision for entry in register.entries):
        raise RightsPolicyError("entries enthält keine exakten RightsDecision-Werte")
    for entry in register.entries:
        _validate_decision_instance(entry, require_hash=True)
    keys = tuple(_approval_tuple(entry.approval_key) for entry in register.entries)
    if len(set(keys)) != len(keys):
        raise RightsPolicyError("ApprovalKey ist doppelt")
    if keys != tuple(sorted(keys)):
        raise RightsPolicyError("Registereinträge müssen kanonisch sortiert sein")
    hashes = tuple(entry.decision_sha256 for entry in register.entries)
    if len(set(hashes)) != len(hashes):
        raise RightsPolicyError("decision_sha256 ist doppelt")


def _validate_policy_instance(policy: RightsPolicy) -> None:
    if type(policy) is not RightsPolicy:
        raise RightsPolicyError("RightsPolicy besitzt keinen exakten Typ")
    if type(policy.schema_version) is not int or policy.schema_version != 1:
        raise RightsPolicyError("Rechtepolicy-Version ist nicht fail-closed")
    if (
        type(policy.default_state) is not RightsState
        or policy.default_state is not RightsState.METADATA_ONLY
    ):
        raise RightsPolicyError("default_state ist nicht fail-closed")
    if (
        type(policy.approved_visibilities) is not tuple
        or type(policy.internal_only_visibilities) is not tuple
        or policy.approved_visibilities != _APPROVED_VISIBILITIES
        or policy.internal_only_visibilities != _INTERNAL_VISIBILITIES
        or type(policy.publication_actions) is not tuple
        or policy.publication_actions != _PUBLICATION_ACTIONS
    ):
        raise RightsPolicyError("Rechtepolicy-Version oder -Matrix ist nicht fail-closed")


def _canonical_authority_source() -> Path:
    return Path(DEFAULT_REGISTER_PATH).absolute()


def load_rights_authority() -> RightsAuthority:
    """Mint authority only from current canonical default register source."""

    register_source = _canonical_authority_source()
    load_rights_register(register_source)
    return RightsAuthority(register_source, _token=_AUTHORITY_TOKEN)


def load_fixture_rights_authority(path: Path) -> RightsAuthority:
    """Mint offline-fixture authority with isolated source binding."""

    register_source = Path(path).absolute()
    load_rights_register(register_source)
    return RightsAuthority(register_source, _token=_AUTHORITY_TOKEN, _isolated=True)


def _validate_authority_instance(authority: RightsAuthority) -> None:
    if (
        type(authority) is not RightsAuthority
        or authority._token is not _AUTHORITY_TOKEN
        or not isinstance(authority._register_source, Path)
        or type(authority._isolated) is not bool
        or (
            not authority._isolated
            and authority._register_source != _canonical_authority_source()
        )
    ):
        raise RightsPolicyError(
            "RightsAuthority ist nicht an die kanonische Register-Source gebunden"
        )


def load_authority_register(authority: RightsAuthority) -> RightsRegister:
    """Load register bytes only after validating the opaque authority seal."""

    _validate_authority_instance(authority)
    return load_rights_register(authority._register_source)


def _missing_decision(key: ApprovalKey) -> RightsDecision:
    return RightsDecision(
        approval_key=key,
        state=RightsState.UNKNOWN,
        mode=PublicationMode.ORIGIN_LINK,
        allowed_actions=(),
        components_state=ComponentsState.UNKNOWN,
        attribution=None,
        basis="rights_register_no_match",
        reviewed_by=None,
        reviewed_at=None,
        decision_sha256=None,
    )


def _compatibility_decision(decision: RightsDecision) -> RightsDecision:
    """Project pair-only lookups to metadata without minting effect authority."""

    if decision.mode in {PublicationMode.REMOVE_ALL, PublicationMode.ORIGIN_LINK}:
        return decision
    return replace(
        decision,
        state=RightsState.METADATA_ONLY,
        mode=PublicationMode.SOURCE_ONLY,
        allowed_actions=(),
        components_state=ComponentsState.UNKNOWN,
        attribution=None,
        decision_sha256=None,
    )


def evaluate_rights(
    source_id: str,
    source_sha256: str,
    *,
    register: RightsRegister,
    policy: RightsPolicy,
) -> RightsDecision:
    """Legacy pair lookup for metadata-only callers; never infer authority."""

    _validate_policy_instance(policy)
    _validate_register_instance(register)
    if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
        raise RightsPolicyError("source_id ist keine kanonische RKI-Quell-ID")
    if type(source_sha256) is not str or _SHA256.fullmatch(source_sha256) is None:
        raise RightsPolicyError("source_sha256 muss ein kleingeschriebener SHA-256 sein")
    matches = tuple(
        decision
        for decision in register.entries
        if decision.source_id == source_id and decision.source_sha256 == source_sha256
    )
    if len(matches) > 1:
        raise RightsPolicyError("Legacy-Paarlookup ist für mehrere Bitstreams mehrdeutig")
    if matches:
        return _compatibility_decision(matches[0])
    placeholder = bitstream_identity(
        "https://edoc.rki.de/bitstream/handle/"
        f"{source_id.removeprefix('rki:')}/unknown.pdf"
    )
    return _missing_decision(
        ApprovalKey(
            source_id=source_id,
            canonical_url=placeholder.canonical_url,
            version_or_bitstream=placeholder.bitstream_id,
            source_sha256=source_sha256,
        )
    )


def resolve_rights(
    source_id: str,
    source_sha256: str,
    *,
    authority: RightsAuthority,
    policy: RightsPolicy,
) -> RightsDecision:
    """Reload authority for a compatibility pair lookup."""

    _validate_policy_instance(policy)
    return evaluate_rights(
        source_id,
        source_sha256,
        register=load_authority_register(authority),
        policy=policy,
    )


def resolve_action(
    key: ApprovalKey,
    *,
    action: RightsAction,
    register: RightsRegister,
    policy: RightsPolicy,
) -> RightsDecision:
    """Resolve exact reviewed revision and require one explicit action."""

    _validate_approval_key(key)
    if type(action) is not RightsAction:
        raise RightsPolicyError("action ist keine kanonische RightsAction")
    _validate_register_instance(register)
    _validate_policy_instance(policy)
    decision = next(
        (entry for entry in register.entries if entry.approval_key == key),
        None,
    )
    if decision is None:
        return _missing_decision(key)
    if action not in decision.allowed_actions:
        raise RightsPolicyError(f"allowed_actions autorisiert {action.value} nicht")
    if action not in policy.actions_for_mode(decision.mode):
        raise RightsPolicyError(f"Rechtepolicy autorisiert {action.value} nicht")
    if action is RightsAction.PUBLISH:
        if decision.components_state is not ComponentsState.CLEARED:
            raise RightsPolicyError("components_state blockiert publish")
        if decision.attribution is None:
            raise RightsPolicyError("publish erfordert vollständige Attribution")
        _validate_attribution(decision.attribution)
    return decision


def publication_policy(
    source_id: str,
    source_sha256: str,
    *,
    authority: RightsAuthority,
    visibility: str,
    policy: RightsPolicy,
) -> PublicationPolicy:
    """Derive compatibility projection without creating action authority."""

    if visibility not in _VISIBILITIES:
        raise RightsPolicyError(f"Unbekannte Sichtbarkeit: {visibility!r}")
    decision = resolve_rights(
        source_id,
        source_sha256,
        authority=authority,
        policy=policy,
    )
    actions = decision.allowed_actions if visibility == "public" else ()
    return PublicationPolicy(
        mode=decision.mode,
        allowed_actions=actions,
        visibility=visibility,
    )


_MODE_RANK = {
    PublicationMode.REMOVE_ALL: 0,
    PublicationMode.ORIGIN_LINK: 1,
    PublicationMode.SOURCE_ONLY: 2,
    PublicationMode.MATERIALIZED: 3,
}
_STATE_RANK = {
    RightsState.TAKEDOWN: 0,
    RightsState.UNKNOWN: 1,
    RightsState.METADATA_ONLY: 2,
    RightsState.INTERNAL_ONLY: 3,
    RightsState.APPROVED: 4,
}
_COMPONENT_RANK = {
    ComponentsState.BLOCKED: 0,
    ComponentsState.UNKNOWN: 1,
    ComponentsState.CLEARED: 2,
}


def is_not_more_permissive(
    current: RightsDecision | None,
    previous: RightsDecision,
    *,
    policy: RightsPolicy,
    visibility: str,
) -> bool:
    """Return whether every current public-effect dimension is a subset."""

    _validate_policy_instance(policy)
    if visibility not in _VISIBILITIES:
        raise RightsPolicyError("visibility ist unbekannt")
    _validate_decision_instance(previous, require_hash=False)
    if current is None:
        return True
    _validate_decision_instance(current, require_hash=False)
    if current.approval_key != previous.approval_key:
        raise RightsPolicyError("Restriktionsvergleich benötigt identischen ApprovalKey")
    if visibility != "public":
        return True
    if current.mode is PublicationMode.REMOVE_ALL:
        return True
    if _MODE_RANK[current.mode] > _MODE_RANK[previous.mode]:
        return False
    if _STATE_RANK[current.state] > _STATE_RANK[previous.state]:
        return False
    if _COMPONENT_RANK[current.components_state] > _COMPONENT_RANK[previous.components_state]:
        return False
    if not set(current.allowed_actions).issubset(previous.allowed_actions):
        return False
    if not set(current.allowed_actions).issubset(
        policy.actions_for_mode(current.mode)
    ):
        raise RightsPolicyError("Aktionsmenge überschreitet Rechtepolicy")
    if (
        RightsAction.PUBLISH in current.allowed_actions
        and RightsAction.PUBLISH in previous.allowed_actions
    ):
        current_attribution = current.attribution
        previous_attribution = previous.attribution
        if current_attribution is None or previous_attribution is None:
            raise RightsPolicyError("publish erfordert vollständige Attribution")
        previous_history = previous_attribution.prior_change_history
        current_history = current_attribution.prior_change_history
        if current_history[: len(previous_history)] != previous_history:
            raise RightsPolicyError("prior_change_history muss append-only sein")
        if current_attribution != previous_attribution:
            return False
    equal_effects = (
        current.mode == previous.mode
        and current.state == previous.state
        and current.components_state == previous.components_state
        and current.allowed_actions == previous.allowed_actions
        and current.attribution == previous.attribution
    )
    if equal_effects and (
        current.basis,
        current.reviewed_by,
        current.reviewed_at,
    ) != (
        previous.basis,
        previous.reviewed_by,
        previous.reviewed_at,
    ):
        return False
    return True


def is_monotone_restriction(
    previous: RightsDecision,
    current: RightsDecision | None,
    *,
    policy: RightsPolicy,
    visibility: str,
) -> bool:
    """Return true only for an actual wholly monotone restriction."""

    if not is_not_more_permissive(
        current,
        previous,
        policy=policy,
        visibility=visibility,
    ):
        return False
    if current is None:
        return True
    if visibility != "public":
        return True
    if current.mode is PublicationMode.REMOVE_ALL:
        return previous.mode is not PublicationMode.REMOVE_ALL
    return (
        current.mode != previous.mode
        or current.state != previous.state
        or current.components_state != previous.components_state
        or current.allowed_actions != previous.allowed_actions
        or current.attribution != previous.attribution
    )


__all__ = [
    "ApprovalKey",
    "ComponentsState",
    "PublicationMode",
    "PublicationPolicy",
    "RightsAction",
    "RightsAttribution",
    "RightsAuthority",
    "RightsDecision",
    "RightsPolicy",
    "RightsPolicyError",
    "RightsRegister",
    "RightsState",
    "decision_sha256",
    "evaluate_rights",
    "is_monotone_restriction",
    "is_not_more_permissive",
    "load_authority_register",
    "load_fixture_rights_authority",
    "load_rights_authority",
    "load_rights_policy",
    "load_rights_register",
    "parse_rights_policy",
    "parse_rights_register",
    "publication_policy",
    "resolve_action",
    "resolve_rights",
    "validate_license_url",
]
