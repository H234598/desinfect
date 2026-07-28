"""Deterministic property-matrix tests for canonical paths."""

from __future__ import annotations

from itertools import product

from scripts.rki_pipeline.io_utils import normalize_posix_path, portable_collision_key


def test_safe_path_matrix_is_idempotent_and_portable() -> None:
    """Exercise many portable paths without network or randomized state."""

    components = ("Alpha", "beta", "DATEI_01", "über", "x-y")
    for size in (1, 2, 3):
        for parts in product(components, repeat=size):
            raw = "/".join(parts)
            normalized = normalize_posix_path(raw)
            assert normalize_posix_path(normalized) == normalized
            assert portable_collision_key(normalized) == normalized.casefold()
