"""Regression tests for immutable storage path boundaries."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rki_pipeline.storage.base import StorageConfigurationError
from scripts.rki_pipeline.storage.config import load_storage_config


def config_text(
    *,
    lfs_root: str = "rki/Bulletins",
    release_prefix: str = "rki/Bulletins",
    object_namespace: str = "rki/Bulletins",
) -> str:
    return f'''schema_version = 1
backend = "lfs"

[lfs]
artifact_root = "{lfs_root}"
max_run_objects = 100
max_run_bytes = 1000
warn_total_bytes = 2000
block_total_bytes = 3000

[release]
tag_prefix = "desinfect-archive"
asset_prefix = "{release_prefix}"

[object]
bucket = "desinfect"
namespace = "{object_namespace}"
'''


@pytest.mark.parametrize(
    "text",
    (
        config_text(lfs_root="scripts"),
        config_text(release_prefix="releases/other"),
        config_text(object_namespace="objects/other"),
    ),
)
def test_storage_namespaces_cannot_expand_automatic_write_boundary(
    tmp_path: Path,
    text: str,
) -> None:
    path = tmp_path / "storage.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="rki/Bulletins"):
        load_storage_config(path)
