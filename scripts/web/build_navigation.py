"""Pure validation for the static reader navigation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from scripts.rki_pipeline.io_utils import UnsafePathError, normalize_posix_path
from scripts.web.content_index import ContentIndex


class NavigationError(ValueError):
    """Static reader navigation violates the exact P10.3 contract."""


EXPECTED_NAV: tuple[object, ...] = (
    {"Start": "index.md"},
    {"Handdesinfektion": "Handdesinfektion.md"},
    {"Flächendesinfektion": "Flaechendesinfektion.md"},
    {"Kategorien": "Kategorien.md"},
    {"Anleitungen": [{"Übersicht": "Anleitungen/index.md"}]},
    {"Sortierbare Tabelle": "Tabelle.md"},
    {"Bulletins": [{"Übersicht": "Bulletins/index.md"}]},
    {
        "Methodik": [
            {"Wirksamkeit": "Methodik/Wirksamkeit.md"},
            {"Bewertung": "Methodik/Bewertung.md"},
            {"Sicherheit": "Methodik/Sicherheit.md"},
        ]
    },
)
EXPECTED_NOT_IN_NAV = "/WARTUNG.md\n/Wartung/\n"


def _nav_targets(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise NavigationError("mkdocs nav must be a non-empty list")
    targets: list[str] = []
    for item in value:
        if not isinstance(item, dict) or len(item) != 1:
            raise NavigationError("each mkdocs nav entry must have exactly one label")
        label, target = next(iter(item.items()))
        if not isinstance(label, str) or not label.strip():
            raise NavigationError("mkdocs nav labels must be non-empty strings")
        if isinstance(target, list):
            targets.extend(_nav_targets(target))
        elif isinstance(target, str):
            targets.append(target)
        else:
            raise NavigationError(f"mkdocs nav target for {label!r} is invalid")
    return tuple(targets)


def _validate_target(target: str, index: ContentIndex) -> None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("//"):
        raise NavigationError(f"external navigation target is forbidden: {target!r}")
    if PurePosixPath(target).is_absolute():
        raise NavigationError(f"absolute navigation target is forbidden: {target!r}")
    try:
        normalized = normalize_posix_path(target)
    except UnsafePathError as exc:
        raise NavigationError(f"unsafe navigation target: {target!r}") from exc
    if normalized != target or not target.endswith(".md"):
        raise NavigationError(f"non-canonical Markdown navigation target: {target!r}")
    if target == "WARTUNG.md" or target.startswith("Wartung/"):
        raise NavigationError(f"maintenance target must stay outside reader nav: {target}")
    if index.page_for_path(target) is None:
        raise NavigationError(f"navigation target is missing from content index: {target}")


def validate_navigation(config: Mapping[object, object], index: ContentIndex) -> None:
    """Raise unless labels, order, targets and hidden paths are exact."""

    nav = config.get("nav")
    targets = _nav_targets(nav)
    for target in targets:
        _validate_target(target, index)
    if len(targets) != len(set(targets)):
        raise NavigationError("duplicate navigation target")
    if nav != list(EXPECTED_NAV):
        raise NavigationError("reader navigation labels, order or targets differ from P10.3")
    if config.get("not_in_nav") != EXPECTED_NOT_IN_NAV:
        raise NavigationError("maintenance pages must use the exact not_in_nav patterns")


def runtime_navigation_config(
    config: dict[object, object], *, partial: bool
) -> dict[object, object]:
    """Return a deep copy; remove only nav for a partial MkDocs preview."""

    projected = deepcopy(config)
    if partial:
        projected.pop("nav", None)
    return projected
