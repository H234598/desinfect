"""Tests for the reproducible Python/Node package foundation."""

from scripts.validate_dependency_locks import validate_node_lock, validate_python_locks


def test_python_dependency_locks() -> None:
    """Keep pyproject intent and exact lock files synchronized."""

    validate_python_locks()


def test_node_dependency_lock() -> None:
    """Keep Node 24/npm 11 engine intent synchronized with package-lock.json."""

    validate_node_lock()
