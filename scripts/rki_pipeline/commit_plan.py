#!/usr/bin/env python3
"""Deterministic commit messages and content-addressed staged-tree contracts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable

from scripts.rki_pipeline.io_utils import detect_path_collisions, normalize_posix_path

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = frozenset({"100644", "100755"})
_TASK_ID = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._:-]+$")


class CommitPlanError(ValueError):
    """The proposed commit is empty, ambiguous, or non-canonical."""


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One exact repository path, executable mode, and content SHA-256."""

    path: str
    mode: str
    sha256: str

    def __post_init__(self) -> None:
        normalized = normalize_posix_path(self.path)
        if normalized != self.path:
            raise CommitPlanError(f"TreeEntry-Pfad ist nicht kanonisch: {self.path}")
        if self.mode not in _MODES:
            raise CommitPlanError(f"Unzulässiger Gitmodus: {self.mode}")
        if type(self.sha256) is not str or _SHA256.fullmatch(self.sha256) is None:
            raise CommitPlanError("TreeEntry.sha256 muss lowercase 64-hex sein")

    def canonical_row(self) -> bytes:
        return f"{self.mode}\0{self.path}\0{self.sha256}\n".encode("utf-8")


@dataclass(frozen=True, slots=True)
class CommitPlan:
    """One exact base, staged tree, and internal-only commit message."""

    expected_base_sha: str
    entries: tuple[TreeEntry, ...]
    task_ids: tuple[str, ...]
    dispatch_plan_sha256: str
    subject: str
    body: str
    tree_sha256: str

    def __post_init__(self) -> None:
        if type(self.expected_base_sha) is not str or _SHA40.fullmatch(self.expected_base_sha) is None:
            raise CommitPlanError("expected_base_sha muss lowercase 40-hex sein")
        if type(self.entries) is not tuple or not self.entries:
            raise CommitPlanError("CommitPlan benötigt mindestens einen TreeEntry")
        paths = [entry.path for entry in self.entries]
        detect_path_collisions(paths)
        if len(paths) != len(set(paths)):
            raise CommitPlanError("CommitPlan enthält doppelte Pfade")
        if self.entries != tuple(sorted(self.entries, key=lambda entry: entry.path)):
            raise CommitPlanError("CommitPlan-Einträge sind nicht kanonisch sortiert")
        if type(self.task_ids) is not tuple or not self.task_ids:
            raise CommitPlanError("CommitPlan benötigt mindestens eine task_id")
        if self.task_ids != tuple(sorted(set(self.task_ids))):
            raise CommitPlanError("task_ids müssen eindeutig und sortiert sein")
        if any(_TASK_ID.fullmatch(value) is None for value in self.task_ids):
            raise CommitPlanError("CommitPlan enthält ungültige task_id")
        if _SHA256.fullmatch(self.dispatch_plan_sha256) is None:
            raise CommitPlanError("dispatch_plan_sha256 muss lowercase 64-hex sein")
        if _SHA256.fullmatch(self.tree_sha256) is None or self.tree_sha256 != compute_tree_sha256(self.entries):
            raise CommitPlanError("tree_sha256 stimmt nicht mit den Einträgen überein")
        expected_subject = f"chore(rki): apply {len(self.task_ids)} scheduled task(s)"
        if self.subject != expected_subject:
            raise CommitPlanError("Commitbetreff ist nicht kanonisch")
        expected_body = render_body(self.task_ids, self.dispatch_plan_sha256)
        if self.body != expected_body:
            raise CommitPlanError("Committext ist nicht kanonisch")

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    def message(self) -> str:
        return f"{self.subject}\n\n{self.body}\n"


def compute_tree_sha256(entries: Iterable[TreeEntry]) -> str:
    """Hash canonical path/mode/content rows independent of input ordering."""

    materialized = tuple(sorted(tuple(entries), key=lambda entry: entry.path))
    if not materialized:
        raise CommitPlanError("Leerer Baum besitzt keinen CommitPlan-Hash")
    return hashlib.sha256(b"".join(entry.canonical_row() for entry in materialized)).hexdigest()


def render_body(task_ids: tuple[str, ...], dispatch_plan_sha256: str) -> str:
    lines = ["Tasks:", *(f"- {task_id}" for task_id in task_ids), "", f"Dispatch-Plan-SHA256: {dispatch_plan_sha256}"]
    return "\n".join(lines)


def build_commit_plan(
    *,
    expected_base_sha: str,
    entries: Iterable[TreeEntry],
    task_ids: Iterable[str],
    dispatch_plan_sha256: str,
) -> CommitPlan:
    """Build a canonical plan without accepting external document titles."""

    canonical_entries = tuple(sorted(tuple(entries), key=lambda entry: entry.path))
    canonical_tasks = tuple(sorted(set(task_ids)))
    return CommitPlan(
        expected_base_sha=expected_base_sha,
        entries=canonical_entries,
        task_ids=canonical_tasks,
        dispatch_plan_sha256=dispatch_plan_sha256,
        subject=f"chore(rki): apply {len(canonical_tasks)} scheduled task(s)",
        body=render_body(canonical_tasks, dispatch_plan_sha256),
        tree_sha256=compute_tree_sha256(canonical_entries),
    )
