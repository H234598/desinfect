"""Tests for the reproducible Python/Node package foundation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_dependency_locks import (
    LOCK_METADATA,
    validate_node_lock,
    validate_python_locks,
    validate_resolver_report,
)


def test_python_dependency_locks() -> None:
    """Keep direct intent and exact transitive closures synchronized."""

    validate_python_locks()


def test_node_dependency_lock() -> None:
    """Keep Node 24/npm 11 engine intent synchronized with package-lock.json."""

    validate_node_lock()


def _write_report(path: Path, packages: dict[str, str]) -> None:
    """Write a minimal pip report containing one install record per package."""

    report = {
        "install": [
            {"metadata": {"name": name, "version": version}}
            for name, version in packages.items()
        ]
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def test_fresh_resolver_report_must_equal_complete_closure(tmp_path: Path) -> None:
    """Reject a resolver report that omits even one transitive dependency."""

    metadata = json.loads(LOCK_METADATA.read_text(encoding="utf-8"))
    packages = dict(metadata["scopes"]["runtime"]["packages"])
    report = tmp_path / "runtime.json"
    _write_report(report, packages)
    validate_resolver_report("runtime", report)

    packages.pop(next(iter(packages)))
    _write_report(report, packages)
    with pytest.raises(ValueError, match="Resolverbericht driftet"):
        validate_resolver_report("runtime", report)
