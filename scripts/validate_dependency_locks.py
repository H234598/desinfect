#!/usr/bin/env python3
"""Validate Python/Node lock intent and render pip resolver reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$")


def canonical_name(name: str) -> str:
    """Normalize a Python distribution name according to PEP 503."""

    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_lines(path: Path) -> dict[str, str]:
    """Read exact pins and ignore comments plus recursive include directives."""

    pins: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}:{number}: nur exakte name==version-Pins sind erlaubt")
        name = canonical_name(match.group("name"))
        if name in pins:
            raise ValueError(f"{path}:{number}: doppelter Pin für {name}")
        pins[name] = match.group("version")
    if not pins:
        raise ValueError(f"{path}: keine Pins gefunden")
    return pins


def require_sorted(path: Path, pins: dict[str, str]) -> None:
    """Require canonical alphabetical ordering of all effective lock entries."""

    effective = [
        raw.strip()
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.lstrip().startswith("#") and not raw.startswith("-r ")
    ]
    expected = [f"{name}=={pins[name]}" for name in sorted(pins)]
    normalized = []
    for line in effective:
        match = PIN.fullmatch(line)
        assert match is not None
        normalized.append(
            f"{canonical_name(match.group('name'))}=={match.group('version')}"
        )
    if normalized != expected:
        raise ValueError(f"{path}: Pins müssen kanonisch alphabetisch sortiert sein")


def project_pins(group: str | None = None) -> dict[str, str]:
    """Return exact project or optional-dependency pins from pyproject.toml."""

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements: Iterable[str]
    if group is None:
        requirements = data["project"]["dependencies"]
    else:
        requirements = data["project"]["optional-dependencies"][group]
    result: dict[str, str] = {}
    for item in requirements:
        match = PIN.fullmatch(item)
        if match is None:
            raise ValueError(f"pyproject.toml: nicht exakt gepinnte Abhängigkeit: {item}")
        result[canonical_name(match.group("name"))] = match.group("version")
    return result


def require_subset(label: str, expected: dict[str, str], lock: dict[str, str]) -> None:
    """Require exact direct pins to be present in a transitive lock."""

    missing = {name: version for name, version in expected.items() if lock.get(name) != version}
    if missing:
        raise ValueError(f"{label}: direkte Pins fehlen oder weichen ab: {missing}")


def validate_python_locks() -> None:
    """Validate intent files, exact transitive locks, and pyproject parity."""

    runtime_intent = parse_requirement_lines(ROOT / "requirements.in")
    test_intent = parse_requirement_lines(ROOT / "requirements-test.in")
    docs_intent = parse_requirement_lines(ROOT / "requirements-docs.in")
    runtime_lock = parse_requirement_lines(ROOT / "requirements.txt")
    test_lock = parse_requirement_lines(ROOT / "requirements-test.txt")
    docs_lock = parse_requirement_lines(ROOT / "requirements-docs.txt")

    for name, pins in (
        ("requirements.txt", runtime_lock),
        ("requirements-test.txt", test_lock),
        ("requirements-docs.txt", docs_lock),
    ):
        require_sorted(ROOT / name, pins)

    if runtime_intent != project_pins():
        raise ValueError("requirements.in und project.dependencies sind nicht identisch")
    if test_intent != project_pins("test"):
        raise ValueError("requirements-test.in und project.optional-dependencies.test weichen ab")
    if docs_intent != project_pins("docs"):
        raise ValueError("requirements-docs.in und project.optional-dependencies.docs weichen ab")

    require_subset("runtime lock", runtime_intent, runtime_lock)
    require_subset("test lock runtime", runtime_intent, test_lock)
    require_subset("test lock direct", test_intent, test_lock)
    require_subset("docs lock runtime", runtime_intent, docs_lock)
    require_subset("docs lock direct", docs_intent, docs_lock)

    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    if build.get("requires") != ["setuptools==83.0.0"]:
        raise ValueError("Buildbackend muss exakt auf setuptools==83.0.0 gepinnt sein")


def validate_node_lock() -> None:
    """Validate Node 24/npm 11 intent and a dependency-free root lock."""

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    expected_engines = {"node": ">=24 <25", "npm": ">=11 <12"}
    if package.get("engines") != expected_engines:
        raise ValueError("package.json muss Node 24 und npm 11 exakt begrenzen")
    root = lock.get("packages", {}).get("")
    if not isinstance(root, dict) or root.get("engines") != expected_engines:
        raise ValueError("package-lock.json widerspricht den Node-/npm-Engines")
    if lock.get("lockfileVersion") != 3:
        raise ValueError("package-lock.json muss lockfileVersion 3 verwenden")
    if package.get("dependencies") or package.get("devDependencies"):
        raise ValueError("P01 darf noch keine Node-Abhängigkeiten einführen")


def render_report(path: Path) -> str:
    """Render a pip ``--report`` JSON document as a canonical exact lock."""

    report = json.loads(path.read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for item in report.get("install", []):
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("pip report enthält einen Eintrag ohne Name/Version")
        canonical = canonical_name(name)
        if canonical in pins and pins[canonical] != version:
            raise ValueError(f"pip report enthält widersprüchliche Versionen für {canonical}")
        pins[canonical] = version
    if not pins:
        raise ValueError("pip report enthält keine Installationsauflösung")
    return "".join(f"{name}=={pins[name]}\n" for name in sorted(pins))


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--render-report", type=Path)
    return parser


def main() -> None:
    """Validate repository locks or render one resolver report."""

    args = build_parser().parse_args()
    if args.render_report is not None:
        sys.stdout.write(render_report(args.render_report))
        return
    validate_python_locks()
    validate_node_lock()
    print("dependency locks: ok; Python>=3.12; Node>=24<25; npm>=11<12")


if __name__ == "__main__":
    main()
