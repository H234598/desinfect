"""Fail-closed rights decisions for RKI source payloads."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
import tomllib
from typing import Any

import yaml

from scripts.rki_pipeline.io_utils import sha256_bytes, stable_json_dumps


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "rights-policy.toml"
DEFAULT_REGISTER_PATH = ROOT / "research" / "rights-register.yml"
MAX_POLICY_BYTES = 64 * 1024
MAX_REGISTER_BYTES = 1024 * 1024
_SOURCE_ID = re.compile(r"^rki:176904/[0-9]+(?:\.(?:[2-9]|[1-9][0-9]+))?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VISIBILITIES = ("public", "repository_authorized", "internal", "restricted")
_APPROVED_VISIBILITIES = _VISIBILITIES
_INTERNAL_VISIBILITIES = ("internal", "restricted")
_ENTRY_KEYS = frozenset(
    {"source_id", "source_sha256", "state", "basis", "reviewed_by", "reviewed_at"}
)


class RightsPolicyError(ValueError):
    """Rights configuration or an authority lookup is unsafe or malformed."""


class RightsState(StrEnum):
    """Closed set of reviewed and fail-closed rights states."""

    APPROVED = "approved"
    METADATA_ONLY = "metadata_only"
    INTERNAL_ONLY = "internal_only"
    UNKNOWN = "unknown"
    TAKEDOWN = "takedown"


_SENSITIVE_STATES = frozenset(
    {RightsState.APPROVED, RightsState.INTERNAL_ONLY, RightsState.TAKEDOWN}
)


@dataclass(frozen=True, slots=True)
class RightsPolicy:
    """Immutable publication matrix loaded from reviewed TOML."""

    schema_version: int
    default_state: RightsState
    approved_visibilities: tuple[str, ...]
    internal_only_visibilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RightsDecision:
    """One decision bound to exact source identity and bytes."""

    source_id: str
    source_sha256: str
    state: RightsState
    basis: str
    reviewed_by: str | None
    reviewed_at: str | None
    decision_sha256: str | None


@dataclass(frozen=True, slots=True)
class RightsRegister:
    """Immutable reviewed register entries."""

    schema_version: int
    entries: tuple[RightsDecision, ...]


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    """Publication effects allowed by one current rights decision."""

    payload_allowed: bool
    artifact_reference_allowed: bool
    metadata_allowed: bool
    origin_link_allowed: bool


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
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _read_text(path: Path, *, maximum: int, label: str) -> str:
    path = Path(path)
    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise RightsPolicyError(f"{label} ist keine reguläre Datei: {path}")
        if metadata.st_size > maximum:
            raise RightsPolicyError(f"{label} ist zu groß: {path}")
        return path.read_text(encoding="utf-8")
    except RightsPolicyError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RightsPolicyError(f"{label} ist nicht lesbar: {path}") from exc


def _exact_keys(value: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    keys = set(value)
    if keys != expected:
        extra = sorted(repr(key) for key in keys - expected)
        raise RightsPolicyError(
            f"{label} besitzt falsche Schlüssel; missing={sorted(expected - keys)}; "
            f"extra={extra}"
        )


def _policy_matrix(data: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(data, dict):
        raise RightsPolicyError("[fulltext_visibility] muss eine TOML-Tabelle sein")
    expected = frozenset(state.value for state in RightsState)
    _exact_keys(data, expected, label="fulltext_visibility")
    for state in expected:
        if not isinstance(data[state], list) or not all(
            type(value) is str for value in data[state]
        ):
            raise RightsPolicyError(f"fulltext_visibility.{state} muss Stringliste sein")
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


def load_rights_policy(path: Path = DEFAULT_POLICY_PATH) -> RightsPolicy:
    """Load reviewed rights rules."""

    text = _read_text(path, maximum=MAX_POLICY_BYTES, label="Rechtepolicy")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RightsPolicyError(f"Rechtepolicy ist kein gültiges TOML: {path}") from exc
    if not isinstance(data, dict):
        raise RightsPolicyError("Rechtepolicywurzel muss eine TOML-Tabelle sein")
    _exact_keys(
        data,
        frozenset({"schema_version", "default_state", "fulltext_visibility"}),
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
        raise RightsPolicyError("Rights-Register ist kein eindeutiges Safe-YAML") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise RightsPolicyError("Rights-Register muss exakt ein YAML-Objekt enthalten")
    return documents[0]


def _required_string(
    value: object,
    name: str,
    *,
    maximum: int,
) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise RightsPolicyError(f"{name} muss eine nichtleere Zeichenkette sein")
    return value


def _optional_string(value: object, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_string(value, name, maximum=maximum)


def _reviewed_at(value: object) -> str | None:
    reviewed_at = _optional_string(value, "reviewed_at", maximum=40)
    if reviewed_at is None:
        return None
    if not reviewed_at.endswith("Z"):
        raise RightsPolicyError("reviewed_at muss ein UTC-Zeitpunkt mit Z sein")
    try:
        parsed = datetime.fromisoformat(reviewed_at[:-1] + "+00:00")
    except ValueError as exc:
        raise RightsPolicyError("reviewed_at ist kein gültiger UTC-Zeitpunkt") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise RightsPolicyError("reviewed_at muss UTC sein")
    return reviewed_at


def _decision_hash(
    *,
    policy_version: int,
    source_id: str,
    source_sha256: str,
    state: RightsState,
    basis: str,
    reviewed_by: str | None,
    reviewed_at: str | None,
) -> str:
    payload = {
        "policy_version": policy_version,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "state": state.value,
        "basis": basis,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
    }
    return sha256_bytes(stable_json_dumps(payload).encode("utf-8"))


def _register_decision(value: object) -> RightsDecision:
    if not isinstance(value, dict):
        raise RightsPolicyError("Jede Registerentscheidung muss ein Objekt sein")
    _exact_keys(value, _ENTRY_KEYS, label="Registerentscheidung")
    source_id = _required_string(value["source_id"], "source_id", maximum=200)
    if _SOURCE_ID.fullmatch(source_id) is None:
        raise RightsPolicyError("source_id ist keine kanonische RKI-Quell-ID")
    source_sha256 = _required_string(
        value["source_sha256"], "source_sha256", maximum=64
    )
    if _SHA256.fullmatch(source_sha256) is None:
        raise RightsPolicyError("source_sha256 muss ein kleingeschriebener SHA-256 sein")
    try:
        raw_state = RightsState(value["state"])
    except (TypeError, ValueError) as exc:
        raise RightsPolicyError("state ist kein bekannter Rechtezustand") from exc
    basis = _required_string(value["basis"], "basis", maximum=1000)
    reviewed_by = _optional_string(value["reviewed_by"], "reviewed_by", maximum=200)
    reviewed_at = _reviewed_at(value["reviewed_at"])
    if raw_state in _SENSITIVE_STATES:
        if not basis:
            raise RightsPolicyError("basis fehlt für autorisierungssensitiven Zustand")
        if reviewed_by is None:
            raise RightsPolicyError("reviewed_by fehlt für autorisierungssensitiven Zustand")
        if reviewed_at is None:
            raise RightsPolicyError("reviewed_at fehlt für autorisierungssensitiven Zustand")
    effective_state = (
        RightsState.METADATA_ONLY if raw_state is RightsState.UNKNOWN else raw_state
    )
    return RightsDecision(
        source_id=source_id,
        source_sha256=source_sha256,
        state=effective_state,
        basis=basis,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        decision_sha256=_decision_hash(
            policy_version=1,
            source_id=source_id,
            source_sha256=source_sha256,
            state=effective_state,
            basis=basis,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
        ),
    )


def load_rights_register(path: Path = DEFAULT_REGISTER_PATH) -> RightsRegister:
    """Load reviewed source decisions."""

    text = _read_text(path, maximum=MAX_REGISTER_BYTES, label="Rights-Register")
    data = _yaml_document(text)
    _exact_keys(data, frozenset({"schema_version", "decisions"}), label="Rights-Register")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise RightsPolicyError("Unbekannte Rights-Register-Version")
    values = data["decisions"]
    if not isinstance(values, list):
        raise RightsPolicyError("decisions muss eine Liste sein")
    entries: list[RightsDecision] = []
    keys: set[tuple[str, str]] = set()
    for value in values:
        decision = _register_decision(value)
        key = (decision.source_id, decision.source_sha256)
        if key in keys:
            raise RightsPolicyError(f"Autoritätstupel ist doppelt: {key!r}")
        keys.add(key)
        entries.append(decision)
    entries.sort(key=lambda item: (item.source_id, item.source_sha256))
    return RightsRegister(schema_version=1, entries=tuple(entries))


def evaluate_rights(
    source_id: str,
    source_sha256: str,
    *,
    register: RightsRegister,
    policy: RightsPolicy,
) -> RightsDecision:
    """Resolve only the exact source tuple against authoritative entries."""

    _validate_policy_instance(policy)
    if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
        raise RightsPolicyError("source_id ist keine kanonische RKI-Quell-ID")
    if type(source_sha256) is not str or _SHA256.fullmatch(source_sha256) is None:
        raise RightsPolicyError("source_sha256 muss ein kleingeschriebener SHA-256 sein")
    for decision in register.entries:
        if (
            decision.source_id == source_id
            and decision.source_sha256 == source_sha256
        ):
            return decision
    return RightsDecision(
        source_id=source_id,
        source_sha256=source_sha256,
        state=RightsState.METADATA_ONLY,
        basis="rights_register_no_match",
        reviewed_by=None,
        reviewed_at=None,
        decision_sha256=None,
    )


def _validate_policy_instance(policy: RightsPolicy) -> None:
    if (
        policy.schema_version != 1
        or policy.default_state is not RightsState.METADATA_ONLY
        or policy.approved_visibilities != _APPROVED_VISIBILITIES
        or policy.internal_only_visibilities != _INTERNAL_VISIBILITIES
    ):
        raise RightsPolicyError("Rechtepolicy-Version oder -Matrix ist nicht fail-closed")


def publication_policy(
    source_id: str,
    source_sha256: str,
    *,
    register: RightsRegister,
    visibility: str,
    policy: RightsPolicy,
) -> PublicationPolicy:
    """Resolve one exact source tuple and map it to safe publication effects."""

    _validate_policy_instance(policy)
    if visibility not in _VISIBILITIES:
        raise RightsPolicyError(f"Unbekannte Sichtbarkeit: {visibility!r}")
    decision = evaluate_rights(
        source_id,
        source_sha256,
        register=register,
        policy=policy,
    )
    allowed = (
        decision.state is RightsState.APPROVED
        and visibility in policy.approved_visibilities
    ) or (
        decision.state is RightsState.INTERNAL_ONLY
        and visibility in policy.internal_only_visibilities
    )
    return PublicationPolicy(
        payload_allowed=allowed,
        artifact_reference_allowed=allowed,
        metadata_allowed=True,
        origin_link_allowed=True,
    )


__all__ = [
    "PublicationPolicy",
    "RightsDecision",
    "RightsPolicy",
    "RightsPolicyError",
    "RightsRegister",
    "RightsState",
    "evaluate_rights",
    "load_rights_policy",
    "load_rights_register",
    "publication_policy",
]
