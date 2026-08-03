"""Contracts for deterministic staged-tree and commit messages."""
from __future__ import annotations

import pytest

from scripts.rki_pipeline.commit_plan import (
    CommitPlanError,
    TreeEntry,
    build_commit_plan,
    compute_tree_sha256,
)
from scripts.rki_pipeline.io_utils import PathCollisionError


def entry(path: str, payload: str = "a", mode: str = "100644") -> TreeEntry:
    return TreeEntry(path=path, mode=mode, sha256=payload * 64)


def test_tree_hash_is_order_independent() -> None:
    first = (entry("status.json", "a"), entry("rki/Bulletins/a.pdf", "b"))
    second = tuple(reversed(first))
    assert compute_tree_sha256(first) == compute_tree_sha256(second)


def test_tree_hash_changes_with_mode_or_content() -> None:
    base = compute_tree_sha256((entry("status.json", "a"),))
    assert base != compute_tree_sha256((entry("status.json", "b"),))
    assert base != compute_tree_sha256((entry("status.json", "a", "100755"),))


def test_commit_plan_is_canonical_and_internal_only() -> None:
    value = build_commit_plan(
        expected_base_sha="a" * 40,
        entries=(entry("status.json", "b"), entry("rki/Bulletins/a.pdf", "c")),
        task_ids=("year:2025", "week:2026-W30"),
        dispatch_plan_sha256="d" * 64,
    )
    assert value.changed_paths == ("rki/Bulletins/a.pdf", "status.json")
    assert value.subject == "chore(rki): apply 2 scheduled task(s)"
    assert "year:2025" in value.body
    assert "Dispatch-Plan-SHA256: " + "d" * 64 in value.body
    assert "RKI title" not in value.message()


def test_commit_plan_rejects_empty_and_colliding_paths() -> None:
    with pytest.raises(CommitPlanError, match="Leerer"):
        compute_tree_sha256(())
    with pytest.raises(PathCollisionError):
        build_commit_plan(
            expected_base_sha="a" * 40,
            entries=(entry("A.txt", "a"), entry("a.txt", "b")),
            task_ids=("year:2025",),
            dispatch_plan_sha256="d" * 64,
        )


@pytest.mark.parametrize(
    "kwargs",
    (
        {"expected_base_sha": "A" * 40},
        {"dispatch_plan_sha256": "x" * 64},
        {"task_ids": ("bad task",)},
    ),
)
def test_commit_plan_rejects_invalid_machine_fields(kwargs: dict[str, object]) -> None:
    values = {
        "expected_base_sha": "a" * 40,
        "entries": (entry("status.json", "b"),),
        "task_ids": ("year:2025",),
        "dispatch_plan_sha256": "d" * 64,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        build_commit_plan(**values)
