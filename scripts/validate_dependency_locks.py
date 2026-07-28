#!/usr/bin/env python3
"""Validate exact Python/Node locks and fresh pip resolver reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
LOCK_METADATA = ROOT / "config" / "python-locks.json"
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$")
SCOPES = {
    "runtime": ("requirements.in", "requirements.txt", None),
    "test": ("requirements-test.in", "requirements-test.txt", "test"),
    "docs": ("requirements-docs.in", "requirements-docs.txt", "docs"),
}


def canonical_name(name: str) -> str:
    """Normalize a Python distribution name according to PEP 503."""

    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_path(path: Path) -> str:
    """Return a file SHA-256 digest for lock provenance validation."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def require_direct_pins(label: str, expected: dict[str, str], lock: dict[str, str]) -> None:
    """Require every exact direct intent inside a complete transitive closure."""

    missing = {name: version for name, version in expected.items() if lock.get(name) != version}
    if missing:
        raise ValueError(f"{label}: direkte Pins fehlen oder weichen ab: {missing}")


def load_lock_metadata(path: Path = LOCK_METADATA) -> dict[str, object]:
    """Load the reviewed resolver-closure manifest."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0.0":
        raise ValueError("config/python-locks.json besitzt eine unbekannte schema_version")
    if data.get("python_version") != "3.12" or data.get("pip_version") != "26.1.2":
        raise ValueError("Python-/pip-Resolverbasis weicht von der P01-Baseline ab")
    scopes = data.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != set(SCOPES):
        raise ValueError("python-locks.json muss exakt runtime, test und docs enthalten")
    return data


def report_pins(path: Path) -> dict[str, str]:
    """Extract the complete selected package set from a pip ``--report`` file."""

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
    return pins


def _metadata_scope(data: dict[str, object], scope: str) -> dict[str, object]:
    """Return one validated scope object from the closure manifest."""

    scopes = data["scopes"]
    assert isinstance(scopes, dict)
    value = scopes.get(scope)
    if not isinstance(value, dict):
        raise ValueError(f"python-locks.json: ungültiger Scope {scope}")
    return value


def validate_python_locks() -> None:
    """Validate direct intent and exact reviewed transitive resolver closures."""

    metadata = load_lock_metadata()
    runtime_intent = parse_requirement_lines(ROOT / "requirements.in")
    project_runtime = project_pins()
    if runtime_intent != project_runtime:
        raise ValueError("requirements.in und project.dependencies sind nicht identisch")

    for scope, (intent_name, lock_name, project_group) in SCOPES.items():
        intent_path = ROOT / intent_name
        lock_path = ROOT / lock_name
        intent = parse_requirement_lines(intent_path)
        lock = parse_requirement_lines(lock_path)
        require_sorted(lock_path, lock)

        expected_project = project_runtime if project_group is None else project_pins(project_group)
        if intent != expected_project:
            raise ValueError(
                f"{intent_name} und project.optional-dependencies.{project_group} weichen ab"
            )

        scope_metadata = _metadata_scope(metadata, scope)
        if scope_metadata.get("intent_path") != intent_name:
            raise ValueError(f"python-locks.json: falscher intent_path für {scope}")
        if scope_metadata.get("lock_path") != lock_name:
            raise ValueError(f"python-locks.json: falscher lock_path für {scope}")
        if scope_metadata.get("intent_sha256") != sha256_path(intent_path):
            raise ValueError(f"python-locks.json: Intent-Hash driftet für {scope}")
        if scope_metadata.get("lock_sha256") != sha256_path(lock_path):
            raise ValueError(f"python-locks.json: Lock-Hash driftet für {scope}")
        packages = scope_metadata.get("packages")
        if not isinstance(packages, dict) or not all(
            isinstance(name, str) and isinstance(version, str)
            for name, version in packages.items()
        ):
            raise ValueError(f"python-locks.json: ungültige Paketclosure für {scope}")
        if lock != packages:
            missing = sorted(set(packages) - set(lock))
            extra = sorted(set(lock) - set(packages))
            changed = sorted(
                name for name in set(lock) & set(packages) if lock[name] != packages[name]
            )
            raise ValueError(
                f"{lock_name}: keine exakte Resolverclosure; "
                f"missing={missing}; extra={extra}; changed={changed}"
            )

        require_direct_pins(f"{scope} lock runtime", runtime_intent, lock)
        require_direct_pins(f"{scope} lock direct", intent, lock)

    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    if build.get("requires") != ["setuptools==83.0.0"]:
        raise ValueError("Buildbackend muss exakt auf setuptools==83.0.0 gepinnt sein")


def validate_resolver_report(scope: str, path: Path) -> None:
    """Require a fresh pip resolver report to equal the reviewed closure exactly."""

    if scope not in SCOPES:
        raise ValueError(f"Unbekannter Resolver-Scope: {scope}")
    metadata = load_lock_metadata()
    expected = _metadata_scope(metadata, scope).get("packages")
    assert isinstance(expected, dict)
    actual = report_pins(path)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(actual) & set(expected) if actual[name] != expected[name]
        )
        raise ValueError(
            f"Frischer Resolverbericht driftet für {scope}; "
            f"missing={missing}; extra={extra}; changed={changed}"
        )


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

    pins = report_pins(path)
    return "".join(f"{name}=={pins[name]}\n" for name in sorted(pins))


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render-report", type=Path)
    group.add_argument("--verify-report", nargs=2, metavar=("SCOPE", "PATH"))
    return parser


def main() -> None:
    """Validate repository locks, render, or verify a resolver report."""

    args = build_parser().parse_args()
    if args.render_report is not None:
        sys.stdout.write(render_report(args.render_report))
        return
    if args.verify_report is not None:
        scope, raw_path = args.verify_report
        validate_resolver_report(scope, Path(raw_path))
        print(f"resolver report: ok ({scope})")
        return
    validate_python_locks()
    validate_node_lock()
    print("dependency locks: exact resolver closures; Python>=3.12; Node>=24<25; npm>=11<12")


if __name__ == "__main__":
    main()
