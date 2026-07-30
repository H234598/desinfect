#!/usr/bin/env python3
"""Load the immutable RKI source boundary from TOML without side effects."""
from __future__ import annotations

from math import isfinite
from pathlib import Path
import tomllib
from typing import Any

from scripts.rki_grabber.models import SourceConfig

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "rki-source.toml"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one TOML table or reject a non-table value."""

    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(
            f"TOML-Bereich [{name}] muss eine Tabelle sein"
        )
    return value


def _bool(value: Any, name: str, default: bool) -> bool:
    """Return an exact TOML Boolean or its default."""

    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError(
            f"{name} muss ein boolescher TOML-Wert sein"
        )
    return value


def _int(value: Any, name: str, default: int) -> int:
    """Return an exact TOML integer or its default."""

    if value is None:
        return default
    if type(value) is not int:
        raise ValueError(f"{name} muss eine Ganzzahl sein")
    return value


def _float(value: Any, name: str, default: float) -> float:
    """Return one finite TOML number with normalized validation errors."""

    if value is None:
        return default
    if type(value) not in {int, float}:
        raise ValueError(f"{name} muss eine endliche Zahl sein")
    try:
        converted = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise ValueError(
            f"{name} muss eine endliche Zahl sein"
        ) from exc
    if not isfinite(converted):
        raise ValueError(f"{name} muss eine endliche Zahl sein")
    return converted


def _string(value: Any, name: str, default: str) -> str:
    """Return an exact TOML string or its default without coercion."""

    if value is None:
        return default
    if type(value) is not str:
        raise ValueError(f"{name} muss eine Zeichenkette sein")
    return value


def load_source_config(
    path: Path = DEFAULT_CONFIG_PATH,
) -> SourceConfig:
    """Load and validate source, network, and byte-limit configuration."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(
            "Unbekannte rki-source.toml-Version"
        )
    source = _table(data, "source")
    network = _table(data, "network")
    limits = _table(data, "limits")

    allowed_hosts = source.get(
        "allowed_hosts",
        ["edoc.rki.de"],
    )
    if allowed_hosts != ["edoc.rki.de"]:
        raise ValueError(
            "source.allowed_hosts darf ausschließlich "
            "edoc.rki.de enthalten"
        )

    return SourceConfig(
        base_url="https://edoc.rki.de",
        issues_root_handle=_string(
            source.get("issues_root_handle"),
            "issues_root_handle",
            "176904/10",
        ),
        articles_handle=_string(
            source.get("articles_handle"),
            "articles_handle",
            "176904/45",
        ),
        allowed_hosts=("edoc.rki.de",),
        user_agent=_string(
            network.get("user_agent"),
            "user_agent",
            "RKI-EpidBull-Research-Downloader/2.0",
        ),
        delay_seconds=_float(
            network.get("delay_seconds"),
            "delay_seconds",
            1.25,
        ),
        timeout_seconds=_float(
            network.get("timeout_seconds"),
            "timeout_seconds",
            60.0,
        ),
        max_redirects=_int(
            network.get("max_redirects"),
            "max_redirects",
            5,
        ),
        max_listing_pages=_int(
            network.get("max_listing_pages"),
            "max_listing_pages",
            10_000,
        ),
        max_html_bytes=_int(
            limits.get("max_html_bytes"),
            "max_html_bytes",
            4 * 1024 * 1024,
        ),
        max_pdf_bytes=_int(
            limits.get("max_pdf_bytes"),
            "max_pdf_bytes",
            256 * 1024 * 1024,
        ),
        robots_max_bytes=_int(
            limits.get("robots_max_bytes"),
            "robots_max_bytes",
            512 * 1024,
        ),
        respect_robots=_bool(
            network.get("respect_robots"),
            "respect_robots",
            True,
        ),
    )
