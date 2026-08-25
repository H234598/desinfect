"""Exact reader navigation and hidden maintenance-section contracts."""

from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import shutil
from urllib.parse import urljoin, urlsplit

import pytest
import yaml

import scripts.web.build_site as build_site_module
from scripts.web.build_navigation import (
    NavigationError,
    runtime_navigation_config,
    validate_navigation,
)
from scripts.web.build_site import build_site
from scripts.web.content_index import build_content_index


EXPECTED_NAV_PATHS = (
    "index.md",
    "Handdesinfektion.md",
    "Flaechendesinfektion.md",
    "Kategorien.md",
    "Anleitungen/index.md",
    "Tabelle.md",
    "Bulletins/index.md",
    "Methodik/Wirksamkeit.md",
    "Methodik/Bewertung.md",
    "Methodik/Sicherheit.md",
)
MAINTENANCE_PATHS = (
    "Wartung/Architektur.md",
    "Wartung/Datenmodell.md",
    "Wartung/Automatisierung.md",
    "Wartung/Wachhunde.md",
    "Wartung/Speicher.md",
    "Wartung/Archivierung-und-Konvertierung.md",
    "Wartung/Reconciliation.md",
    "Wartung/Wiederherstellung.md",
    "Wartung/Sicherheit-und-Betrieb.md",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("content", "config", "web"):
        shutil.copytree(source / name, target / name)
    shutil.copy2(source / "mkdocs.yml", target / "mkdocs.yml")
    shutil.copy2(source / "status.json", target / "status.json")
    return target


def _load_config(repo: Path) -> dict[object, object]:
    parsed = yaml.safe_load((repo / "mkdocs.yml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _flatten_nav(value: object) -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(value, str):
        paths.append(value)
    elif isinstance(value, list):
        for item in value:
            paths.extend(_flatten_nav(item))
    elif isinstance(value, dict):
        for item in value.values():
            paths.extend(_flatten_nav(item))
    return tuple(paths)


def _nav_labels(value: object) -> tuple[str, ...]:
    labels: list[str] = []
    if isinstance(value, list):
        for item in value:
            labels.extend(_nav_labels(item))
    elif isinstance(value, dict):
        labels.extend(str(label) for label in value)
        for item in value.values():
            labels.extend(_nav_labels(item))
    return tuple(labels)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self._active: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._active = {name: value or "" for name, value in attrs}
            self._active["text"] = ""

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active is not None:
            self._active["text"] = self._active["text"].strip()
            self.anchors.append(self._active)
            self._active = None


def _anchors(path: Path) -> list[dict[str, str]]:
    parser = _AnchorParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.anchors


def test_navigation_is_exact_and_maintenance_is_hidden(repo: Path) -> None:
    config = _load_config(repo)
    index = build_content_index(repo / "content")

    validate_navigation(config, index)

    assert _flatten_nav(config["nav"]) == EXPECTED_NAV_PATHS
    assert "Projekt" not in _nav_labels(config["nav"])
    assert "Wartung" not in _nav_labels(config["nav"])
    assert config["not_in_nav"] == "/WARTUNG.md\n/Wartung/\n"


@pytest.mark.parametrize(
    "target",
    (
        "Fehlt.md",
        "index.md",
        "/Handdesinfektion.md",
        "https://example.invalid/page.md",
        "WARTUNG.md",
    ),
)
def test_navigation_rejects_unknown_duplicate_absolute_external_or_hidden_target(
    repo: Path, target: str
) -> None:
    config = _load_config(repo)
    config["nav"][1] = {"Handdesinfektion": target}

    with pytest.raises(NavigationError):
        validate_navigation(config, build_content_index(repo / "content"))


def test_navigation_rejects_missing_target_page(repo: Path) -> None:
    (repo / "content/Handdesinfektion.md").unlink()

    with pytest.raises(NavigationError, match="missing"):
        validate_navigation(_load_config(repo), build_content_index(repo / "content"))


@pytest.mark.parametrize("mutation", ("label", "order"))
def test_navigation_rejects_changed_label_or_order(repo: Path, mutation: str) -> None:
    config = _load_config(repo)
    if mutation == "label":
        config["nav"][1] = {"Hände": "Handdesinfektion.md"}
    else:
        config["nav"][1], config["nav"][2] = config["nav"][2], config["nav"][1]

    with pytest.raises(NavigationError):
        validate_navigation(config, build_content_index(repo / "content"))


def test_runtime_navigation_projection_removes_only_partial_nav(repo: Path) -> None:
    config = _load_config(repo)
    original = deepcopy(config)

    full = runtime_navigation_config(config, partial=False)
    partial = runtime_navigation_config(config, partial=True)

    assert full == original
    assert full is not config
    assert "nav" in full
    assert "nav" not in partial
    assert partial["not_in_nav"] == original["not_in_nav"]
    assert {**partial, "nav": original["nav"]} == original
    assert config == original


def test_strict_site_exposes_cards_tool_link_and_all_maintenance_pages(repo: Path) -> None:
    build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url=None,
    )

    landing = _anchors(repo / "site/index.html")
    cards = {
        anchor["text"]: anchor for anchor in landing if "landing-card" in anchor.get("class", "")
    }
    assert set(cards) == {"Händedesinfektion", "Flächendesinfektion", "Kategorien"}
    assert {urlsplit(anchor["href"]).path for anchor in cards.values()} == {
        "Handdesinfektion/",
        "Flaechendesinfektion/",
        "Kategorien/",
    }

    tool = next(anchor for anchor in landing if "maintenance-tool" in anchor.get("class", ""))
    assert tool["text"] == "🛠️"
    assert tool["title"] == "Wartung, Projekt und Automatisierung"
    assert tool["aria-label"] == "Wartung, Projekt und Automatisierung"
    assert urlsplit(tool["href"]).path == "WARTUNG/"

    index = build_content_index(repo / "content")
    assert all(index.page_for_path(path) is not None for path in MAINTENANCE_PATHS)
    hub = _anchors(repo / "site/WARTUNG/index.html")
    hub_targets = {urljoin("/WARTUNG/", anchor["href"]) for anchor in hub}
    assert {f"/Wartung/{PurePosixPath(path).stem}/" for path in MAINTENANCE_PATHS} <= hub_targets
    assert all(
        (repo / "site" / path.removesuffix(".md") / "index.html").is_file()
        for path in MAINTENANCE_PATHS
    )


def test_partial_strict_preview_keeps_sources_and_publishes_nothing(repo: Path) -> None:
    config_before = (repo / "mkdocs.yml").read_bytes()

    result = build_site(
        repo,
        check=True,
        dry_run=True,
        strict=True,
        force=False,
        site_url=None,
        only=(PurePosixPath("Tabelle.md"),),
    )

    assert result.published is False
    assert "Tabelle/index.html" in result.site_hashes
    assert (repo / "mkdocs.yml").read_bytes() == config_before
    assert not (repo / "build/docs").exists()
    assert not (repo / "site").exists()


def test_partial_site_validation_requires_one_selected_html_page(tmp_path: Path) -> None:
    (tmp_path / "404.html").write_text("not found", encoding="utf-8")

    with pytest.raises(build_site_module.SiteBuildError, match="partial site output"):
        build_site_module._validate_site_tree(tmp_path, partial=True)
