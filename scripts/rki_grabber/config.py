#!/usr/bin/env python3
"""Load the immutable RKI source boundary from TOML without side effects."""
from __future__ import annotations

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
        raise ValueError(f"TOML-Bereich [{name}] muss eine Tabelle sein")
    return value


def load_source_config(path: Path = DEFAULT_CONFIG_PATH) -> SourceConfig:
    """Load and validate source, network, and byte-limit configuration."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unbekannte rki-source.toml-Version")
    source = _table(data, "source")
    network = _table(data, "network")
    limits = _table(data, "limits")
    allowed_hosts = source.get("allowed_hosts", ["edoc.rki.de"])
    if not isinstance(allowed_hosts, list) or any(
        not isinstance(value, str) for value in allowed_hosts
    ):
        raise ValueError("source.allowed_hosts muss eine Stringliste sein")
    return SourceConfig(
        base_url=str(source.get("base_url", "https://edoc.rki.de")),
        issues_root_handle=str(source.get("issues_root_handle", "176904/10")),
        articles_handle=str(source.get("articles_handle", "176904/45")),
        allowed_hosts=tuple(allowed_hosts),
        user_agent=str(
            network.get("user_agent", "RKI-EpidBull-Research-Downloader/2.0")
        ),
        delay_seconds=float(network.get("delay_seconds", 1.25)),
        timeout_seconds=float(network.get("timeout_seconds", 60.0)),
        max_redirects=int(network.get("max_redirects", 5)),
        max_listing_pages=int(network.get("max_listing_pages", 10_000)),
        max_html_bytes=int(limits.get("max_html_bytes", 4 * 1024 * 1024)),
        max_pdf_bytes=int(limits.get("max_pdf_bytes", 256 * 1024 * 1024)),
        robots_max_bytes=int(limits.get("robots_max_bytes", 512 * 1024)),
        respect_robots=bool(network.get("respect_robots", True)),
    )
