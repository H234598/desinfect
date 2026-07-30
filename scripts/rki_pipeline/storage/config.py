#!/usr/bin/env python3
"""Strict TOML configuration for storage adapters and LFS budgets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any

from scripts.rki_pipeline.io_utils import normalize_posix_path
from scripts.rki_pipeline.storage.base import StorageBackend, StorageConfigurationError

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STORAGE_CONFIG = ROOT / "config" / "storage.toml"


@dataclass(frozen=True, slots=True)
class LfsConfig:
    artifact_root: str
    max_run_objects: int
    max_run_bytes: int
    warn_total_bytes: int
    block_total_bytes: int


@dataclass(frozen=True, slots=True)
class ReleaseConfig:
    tag_prefix: str
    asset_prefix: str


@dataclass(frozen=True, slots=True)
class ObjectConfig:
    bucket: str
    namespace: str


@dataclass(frozen=True, slots=True)
class StorageConfig:
    backend: StorageBackend
    lfs: LfsConfig
    release: ReleaseConfig
    object: ObjectConfig


def _table(data: dict[str, Any], name: str, allowed: frozenset[str]) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise StorageConfigurationError(f"[{name}] muss eine TOML-Tabelle sein")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise StorageConfigurationError(f"Unbekannte Schlüssel in [{name}]: {unknown}")
    return value


def _string(table: dict[str, Any], name: str) -> str:
    value = table.get(name)
    if type(value) is not str or not value:
        raise StorageConfigurationError(f"{name} muss eine nichtleere Zeichenkette sein")
    return value


def _positive_int(table: dict[str, Any], name: str) -> int:
    value = table.get(name)
    if type(value) is not int or value <= 0:
        raise StorageConfigurationError(f"{name} muss eine positive Ganzzahl sein")
    return value


def load_storage_config(path: Path = DEFAULT_STORAGE_CONFIG) -> StorageConfig:
    """Load the complete, exact-key storage configuration."""

    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StorageConfigurationError(f"Storage-Konfiguration ist nicht lesbar: {path}") from exc
    allowed_top = {"schema_version", "backend", "lfs", "release", "object"}
    unknown_top = sorted(set(data) - allowed_top)
    if unknown_top:
        raise StorageConfigurationError(f"Unbekannte Top-Level-Schlüssel: {unknown_top}")
    if data.get("schema_version") != 1:
        raise StorageConfigurationError("Unbekannte storage.toml-Version")
    raw_backend = data.get("backend")
    if type(raw_backend) is not str:
        raise StorageConfigurationError("backend muss eine Zeichenkette sein")
    try:
        backend = StorageBackend(raw_backend)
    except ValueError as exc:
        raise StorageConfigurationError(f"Unbekanntes Storage-Backend: {raw_backend}") from exc

    lfs = _table(
        data,
        "lfs",
        frozenset({
            "artifact_root", "max_run_objects", "max_run_bytes",
            "warn_total_bytes", "block_total_bytes",
        }),
    )
    release = _table(data, "release", frozenset({"tag_prefix", "asset_prefix"}))
    object_table = _table(data, "object", frozenset({"bucket", "namespace"}))

    artifact_root = normalize_posix_path(_string(lfs, "artifact_root"))
    max_run_objects = _positive_int(lfs, "max_run_objects")
    max_run_bytes = _positive_int(lfs, "max_run_bytes")
    warn_total_bytes = _positive_int(lfs, "warn_total_bytes")
    block_total_bytes = _positive_int(lfs, "block_total_bytes")
    if warn_total_bytes >= block_total_bytes:
        raise StorageConfigurationError("warn_total_bytes muss unter block_total_bytes liegen")

    return StorageConfig(
        backend=backend,
        lfs=LfsConfig(
            artifact_root=artifact_root,
            max_run_objects=max_run_objects,
            max_run_bytes=max_run_bytes,
            warn_total_bytes=warn_total_bytes,
            block_total_bytes=block_total_bytes,
        ),
        release=ReleaseConfig(
            tag_prefix=_string(release, "tag_prefix"),
            asset_prefix=normalize_posix_path(_string(release, "asset_prefix")),
        ),
        object=ObjectConfig(
            bucket=_string(object_table, "bucket"),
            namespace=normalize_posix_path(_string(object_table, "namespace")),
        ),
    )
