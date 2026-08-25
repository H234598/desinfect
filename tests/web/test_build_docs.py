"""Atomic generated documentation build contracts."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil

import pytest

from scripts.rki_pipeline.io_utils import mark_generated_root
from scripts.rki_pipeline.staging import StagingError
import scripts.web.build_docs as build_docs_module
from scripts.web.build_docs import (
    BuildDocsError,
    build_docs,
    docs_preview_session,
    load_publication_config,
    render_docs_tree,
    snapshot_sources,
)
from scripts.web.link_types import LinkError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Provide an isolated minimal repository copy for each build test."""

    source = Path(__file__).parents[2]
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("content", "config", "web"):
        source_path = source / name
        if source_path.exists():
            shutil.copytree(source_path, target / name)
    for name in ("mkdocs.yml",):
        source_path = source / name
        if source_path.exists():
            shutil.copy2(source_path, target / name)
    return target


def _old_docs(repo: Path) -> Path:
    old = repo / "build/docs"
    mark_generated_root(old, allowed_root=repo)
    (old / "keep.txt").write_text("old complete tree", encoding="utf-8")
    return old


def test_build_docs_publishes_transformed_copy_and_preserves_sources(repo: Path) -> None:
    """Missing conversion, sentinel, or source purity makes this fail."""

    before = snapshot_sources(repo)
    result = build_docs(repo, force=False)
    rendered = (repo / "build/docs/index.md").read_text(encoding="utf-8")

    assert "[Handdesinfektion](Handdesinfektion.md)" in rendered
    assert "[[Handdesinfektion]]" not in rendered
    assert result.published is True
    assert snapshot_sources(repo) == before
    assert (repo / "build/docs/.desinfect-generated").is_file()


def test_build_docs_failure_keeps_old_complete_target(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rendering failure must not replace an already published docs tree."""

    old = _old_docs(repo)
    monkeypatch.setattr(
        build_docs_module,
        "convert_for_web",
        lambda *args, **kwargs: (_ for _ in ()).throw(LinkError("bad link")),
    )

    with pytest.raises(BuildDocsError, match="bad link"):
        build_docs(repo, force=False)

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


def test_build_docs_applies_wikilinks_then_callouts_once_per_page(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordered or duplicate P10.1 transformations make this fail."""

    fixture_page = repo / "content/index.md"
    fixture_page.write_text(
        fixture_page.read_text(encoding="utf-8") + "\n> [!note] Hinweis\n> [[Handdesinfektion]]\n",
        encoding="utf-8",
    )
    calls: list[str] = []
    real_wikilinks = build_docs_module.convert_for_web
    real_callouts = build_docs_module.convert_obsidian_callouts_for_web

    def convert_wikilinks(*args: object, **kwargs: object) -> str:
        calls.append("wikilinks")
        return real_wikilinks(*args, **kwargs)

    def convert_callouts(*args: object, **kwargs: object) -> str:
        calls.append("callouts")
        return real_callouts(*args, **kwargs)

    monkeypatch.setattr(build_docs_module, "convert_for_web", convert_wikilinks)
    monkeypatch.setattr(build_docs_module, "convert_obsidian_callouts_for_web", convert_callouts)

    build_docs(repo, force=False)

    rendered = (repo / "build/docs/index.md").read_text(encoding="utf-8")
    assert '!!! note "Hinweis"' in rendered
    assert "[Handdesinfektion](Handdesinfektion.md)" in rendered
    assert calls == [
        item for _ in sorted((repo / "content").rglob("*.md")) for item in ("wikilinks", "callouts")
    ]


def test_build_docs_rejects_output_symlink_without_replacing_old_docs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink written into stage must abort publication before replacement."""

    old = _old_docs(repo)
    real_copy = build_docs_module.copy_only_regular_local_assets

    def copy_with_symlink(source: Path, destination: Path, **kwargs: object) -> None:
        real_copy(source, destination, **kwargs)
        if source == repo / "content":
            (destination / "escape").symlink_to(repo / "content/index.md")

    monkeypatch.setattr(build_docs_module, "copy_only_regular_local_assets", copy_with_symlink)

    with pytest.raises(BuildDocsError, match="symlink"):
        build_docs(repo, force=False)

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


def test_build_docs_rejects_source_hash_drift_without_replacing_old_docs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source mutation after snapshot must abort publication before replacement."""

    old = _old_docs(repo)
    source = repo / "content/index.md"
    original = source.read_bytes()
    real_render = build_docs_module.render_docs_tree

    def render_with_source_mutation(root: Path, stage: Path, **kwargs: object) -> object:
        real_snapshot = build_docs_module.snapshot_sources
        calls = 0

        def snapshot_then_mutate(snapshot_root: Path) -> object:
            nonlocal calls
            result = real_snapshot(snapshot_root)
            calls += 1
            if calls == 1:
                source.write_bytes(original + b"\nmutation\n")
            return result

        monkeypatch.setattr(build_docs_module, "snapshot_sources", snapshot_then_mutate)
        return real_render(root, stage, **kwargs)

    monkeypatch.setattr(build_docs_module, "render_docs_tree", render_with_source_mutation)

    try:
        with pytest.raises(BuildDocsError, match="source-hash drift"):
            build_docs(repo, force=False)
    finally:
        source.write_bytes(original)

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


def test_build_docs_never_follows_existing_stage_page_symlink(repo: Path, tmp_path: Path) -> None:
    """Following a staged page link would overwrite an external victim."""

    stage = tmp_path / "stage"
    stage.mkdir()
    page_victim = tmp_path / "page-victim.md"
    page_victim.write_bytes(b"page victim")
    (stage / "index.md").symlink_to(page_victim)

    with pytest.raises(BuildDocsError):
        render_docs_tree(repo, stage)

    assert page_victim.read_bytes() == b"page victim"


def test_build_docs_never_follows_existing_stage_asset_symlink(repo: Path, tmp_path: Path) -> None:
    """Following a staged asset link would overwrite an external victim."""

    (repo / "content/asset.txt").write_bytes(b"source asset")
    stage = tmp_path / "stage"
    stage.mkdir()
    asset_victim = tmp_path / "asset-victim.txt"
    asset_victim.write_bytes(b"asset victim")
    (stage / "asset.txt").symlink_to(asset_victim)

    with pytest.raises(BuildDocsError):
        render_docs_tree(repo, stage)

    assert (stage / "index.md").is_file()
    assert asset_victim.read_bytes() == b"asset victim"


def test_build_docs_wraps_staging_error_with_cause(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwrapped staging error would leak the lower-level public API."""

    @contextmanager
    def fail_staging(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise StagingError("staging lock failed")
        yield

    monkeypatch.setattr(build_docs_module, "staged_directory", fail_staging)

    with pytest.raises(BuildDocsError, match="staging lock failed") as raised:
        build_docs(repo, force=False)

    assert isinstance(raised.value.__cause__, StagingError)


def test_build_docs_preview_preserves_caller_exception_and_cleans_up(repo: Path) -> None:
    """Wrapping caller failure would hide its error and leak preview ownership."""

    preview_path: Path | None = None
    with pytest.raises(ValueError, match="caller boom") as raised:
        with docs_preview_session(repo) as preview:
            preview_path = preview.docs_dir
            raise ValueError("caller boom")

    assert type(raised.value) is ValueError
    assert preview_path is not None
    assert not preview_path.exists()


def test_build_docs_rejects_duplicate_publication_config_keys(repo: Path) -> None:
    """Safe YAML loading alone accepts duplicate keys and hides configuration mistakes."""

    config = repo / "config/publication.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original + "site_dir: site\n", encoding="utf-8")

    try:
        with pytest.raises(BuildDocsError, match="duplicate"):
            load_publication_config(repo)
    finally:
        config.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    "merge",
    (
        "<<: {site_dir: hidden/site}\n",
        "defaults: &defaults {site_dir: hidden/site}\n<<: *defaults\n",
    ),
)
def test_build_docs_rejects_publication_config_merge_keys(repo: Path, merge: str) -> None:
    """Merge syntax can hide values before exact-schema validation sees them."""

    config = repo / "config/publication.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original + merge, encoding="utf-8")

    try:
        with pytest.raises(BuildDocsError, match="merge"):
            load_publication_config(repo)
    finally:
        config.write_text(original, encoding="utf-8")
