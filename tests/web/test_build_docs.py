"""Atomic generated documentation build contracts."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
from types import SimpleNamespace

import pytest
import yaml

from scripts.rki_pipeline import io_utils
from scripts.rki_pipeline.io_utils import mark_generated_root
from scripts.rki_pipeline import staging as staging_module
from scripts.rki_pipeline.staging import StagingError
import scripts.web.build_site as build_site_module
import scripts.web.build_docs as build_docs_module
from scripts.web.build_site import SiteBuildError, build_site, main
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


def _mkdocs_source(repo: Path) -> Path:
    """Install minimal static MkDocs input required by site-build tests."""

    path = repo / "mkdocs.yml"
    path.write_text("site_name: Test\n", encoding="utf-8")
    return path


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


def test_build_docs_preview_preserves_caller_exception_after_target_mutation(
    repo: Path,
) -> None:
    """Caller failure must win over no-change validation after target mutation."""

    target = repo / "build/.docs-preview"
    mark_generated_root(target, allowed_root=repo)
    target_file = target / "keep.txt"
    target_file.write_text("before", encoding="utf-8")
    preview_path: Path | None = None

    with pytest.raises(RuntimeError, match="caller boom") as raised:
        with docs_preview_session(repo) as preview:
            preview_path = preview.docs_dir
            target_file.write_text("caller mutation", encoding="utf-8")
            raise RuntimeError("caller boom")

    assert type(raised.value) is RuntimeError
    assert target_file.read_text(encoding="utf-8") == "caller mutation"
    assert preview_path is not None
    assert not preview_path.exists()
    assert not list(target.parent.glob(f".{target.name}.staging-*"))


def test_build_docs_preview_wraps_setup_failure_without_leaking_stage(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview setup failures stay classified while staging owns cleanup."""

    def fail_mark(descriptor: int) -> None:
        del descriptor
        raise OSError("preview setup failed")

    monkeypatch.setattr(staging_module, "mark_generated_root_fd", fail_mark)

    with pytest.raises(BuildDocsError, match="preview setup failed") as raised:
        with docs_preview_session(repo):
            pass

    assert isinstance(raised.value.__cause__, OSError)
    target = repo / "build/.docs-preview"
    assert not list(target.parent.glob(f".{target.name}.staging-*"))


def test_build_docs_preview_wraps_normal_cleanup_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal preview teardown failure remains a classified build error."""

    @contextmanager
    def fail_cleanup(*args: object, **kwargs: object) -> object:
        del args, kwargs
        yield repo / "build"
        raise OSError("preview cleanup failed")

    monkeypatch.setattr(build_docs_module, "preview_directory", fail_cleanup)
    monkeypatch.setattr(
        build_docs_module,
        "render_docs_tree",
        lambda *args, **kwargs: build_docs_module.DocsBuildResult({}, {}, False),
    )

    with pytest.raises(BuildDocsError, match="preview cleanup failed") as raised:
        with docs_preview_session(repo):
            pass

    assert isinstance(raised.value.__cause__, OSError)


def test_build_docs_preview_keeps_caller_exception_when_teardown_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teardown failure is noted without replacing the active caller error."""

    caller = RuntimeError("caller boom")
    preview_path = repo / "build/.docs-preview"

    @contextmanager
    def fail_teardown(*args: object, **kwargs: object) -> object:
        del args, kwargs
        preview_path.mkdir(parents=True)
        try:
            yield preview_path
        finally:
            raise OSError("teardown boom")

    monkeypatch.setattr(build_docs_module, "preview_directory", fail_teardown)
    monkeypatch.setattr(
        build_docs_module,
        "render_docs_tree",
        lambda *args, **kwargs: build_docs_module.DocsBuildResult({}, {}, False),
    )

    with pytest.raises(RuntimeError) as raised:
        with docs_preview_session(repo):
            raise caller

    assert raised.value is caller
    assert "teardown boom" in "\n".join(caller.__notes__)


def test_render_docs_tree_keeps_fd_stage_after_ancestor_swap(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendering through an FD stage cannot redirect writes through a swapped ancestor."""

    stage = repo / "build/stage"
    mark_generated_root(stage, allowed_root=repo)
    outside = tmp_path / "outside"
    outside_stage = outside / "stage"
    outside_stage.mkdir(parents=True)
    mark_generated_root(outside_stage, allowed_root=outside)
    build = repo / "build"
    build_real = repo / "build-real"
    original_open_root = io_utils.open_root_directory
    swapped = False
    stage_path = stage.resolve()

    @contextmanager
    def swap_build_ancestor(path: Path, *, create: bool = False):
        nonlocal swapped
        if not swapped and path in {stage_path, fd_stage}:
            swapped = True
            build.rename(build_real)
            build.symlink_to(outside, target_is_directory=True)
        try:
            with original_open_root(path, create=create) as descriptor:
                yield descriptor
        finally:
            if swapped and build.is_symlink():
                build.unlink()
                build_real.rename(build)

    try:
        with io_utils.open_root_directory(stage) as descriptor:
            fd_stage = io_utils.fd_directory_path(descriptor)
            monkeypatch.setattr(io_utils, "open_root_directory", swap_build_ancestor)
            render_docs_tree(repo, fd_stage)
    finally:
        if build.is_symlink():
            build.unlink()
        if build_real.exists():
            build_real.rename(build)

    assert (build / "stage/index.md").is_file()
    assert not (outside_stage / "index.md").exists()


def test_build_docs_preview_never_creates_build_root_through_injected_symlink(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path-based preview-root creation after config loading can escape the repository."""

    config_path = repo / "config/publication.yaml"
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        original.replace("build_root: build", "build_root: generated/build"),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    real_load = build_docs_module.load_publication_config

    def load_then_inject(root: Path) -> object:
        result = real_load(root)
        (repo / "generated").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(build_docs_module, "load_publication_config", load_then_inject)

    try:
        with pytest.raises(BuildDocsError):
            with docs_preview_session(repo):
                pass
    finally:
        generated = repo / "generated"
        if generated.is_symlink():
            generated.unlink()
        config_path.write_text(original, encoding="utf-8")

    assert not (outside / "build").exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"docs_dir": "build"}, "docs_dir must be strictly beneath build_root"),
        ({"docs_dir": "site"}, "docs_dir must be strictly beneath build_root"),
        ({"content_root": "build/content"}, "content_root overlaps build_root"),
        (
            {
                "content_root": "content",
                "build_root": "content/build",
                "docs_dir": "content/build/docs",
            },
            "content_root overlaps build_root",
        ),
        ({"site_dir": "build/site"}, "site_dir overlaps build_root"),
        (
            {
                "build_root": "site/build",
                "docs_dir": "site/build/docs",
                "site_dir": "site",
            },
            "site_dir overlaps build_root",
        ),
        ({"site_dir": "content/site"}, "site_dir overlaps content_root"),
        (
            {"content_root": "site/content", "site_dir": "site"},
            "site_dir overlaps content_root",
        ),
    ),
)
def test_load_publication_config_rejects_overlapping_output_paths(
    repo: Path, overrides: dict[str, str], message: str
) -> None:
    """Publication roots must not overlap across source and generated trees."""

    config = repo / "config/publication.yaml"
    original = config.read_text(encoding="utf-8")
    defaults = {
        "content_root": "content",
        "build_root": "build",
        "docs_dir": "build/docs",
        "site_dir": "site",
    }
    values = {**defaults, **overrides}
    config.write_text(
        original.replace("content_root: content", f"content_root: {values['content_root']}")
        .replace("build_root: build", f"build_root: {values['build_root']}")
        .replace("docs_dir: build/docs", f"docs_dir: {values['docs_dir']}")
        .replace("site_dir: site", f"site_dir: {values['site_dir']}"),
        encoding="utf-8",
    )

    try:
        with pytest.raises(BuildDocsError, match=message):
            load_publication_config(repo)
    finally:
        config.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        (
            {"build_root": "web/build", "docs_dir": "web/build/docs"},
            "build_root overlaps reserved source root web",
        ),
        (
            {"build_root": "config/build", "docs_dir": "config/build/docs"},
            "build_root overlaps reserved source root config",
        ),
        ({"site_dir": "web/site"}, "site_dir overlaps reserved source root web"),
        ({"site_dir": "config/site"}, "site_dir overlaps reserved source root config"),
        ({"content_root": "web/content"}, "content_root overlaps reserved source root web"),
        (
            {"content_root": "config/content"},
            "content_root overlaps reserved source root config",
        ),
    ),
)
def test_load_publication_config_rejects_reserved_source_root_overlap(
    repo: Path, overrides: dict[str, str], message: str
) -> None:
    """Configured roots cannot consume fixed config or web source trees."""

    config = repo / "config/publication.yaml"
    original = config.read_text(encoding="utf-8")
    defaults = {
        "content_root": "content",
        "build_root": "build",
        "docs_dir": "build/docs",
        "site_dir": "site",
    }
    values = {**defaults, **overrides}
    config.write_text(
        original.replace("content_root: content", f"content_root: {values['content_root']}")
        .replace("build_root: build", f"build_root: {values['build_root']}")
        .replace("docs_dir: build/docs", f"docs_dir: {values['docs_dir']}")
        .replace("site_dir: site", f"site_dir: {values['site_dir']}"),
        encoding="utf-8",
    )

    try:
        with pytest.raises(BuildDocsError, match=message):
            load_publication_config(repo)
    finally:
        config.write_text(original, encoding="utf-8")


def test_load_publication_config_allows_near_prefix_roots(repo: Path) -> None:
    """A sibling such as web-build is not an overlap with fixed web source."""

    config = repo / "config/publication.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(
        original.replace("build_root: build", "build_root: web-build").replace(
            "docs_dir: build/docs", "docs_dir: web-build/docs"
        ),
        encoding="utf-8",
    )

    try:
        loaded = load_publication_config(repo)
    finally:
        config.write_text(original, encoding="utf-8")

    assert loaded.build_root == repo / "web-build"


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


def test_partial_site_build_requires_dry_run(repo: Path) -> None:
    """Partial site selection cannot publish or create build outputs."""

    assert main(["--repo-root", str(repo), "--only", "index.md", "--strict"]) == 2
    assert not (repo / "site").exists()
    assert not (repo / "build/docs").exists()


def test_force_dry_run_is_rejected(repo: Path) -> None:
    """Force has no meaning for disposable preview transactions."""

    with pytest.raises(SiteBuildError, match="force.*dry-run"):
        build_site(
            repo,
            check=True,
            dry_run=True,
            strict=True,
            force=True,
            site_url=None,
        )


def test_site_build_requires_strict_mode(repo: Path) -> None:
    """Every site build mode requires MkDocs strict validation."""

    assert main(["--repo-root", str(repo), "--check"]) == 2


def test_dry_run_never_publishes_docs_or_site(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run consumes live previews and removes both after the runner returns."""

    _mkdocs_source(repo)
    seen: dict[str, Path] = {}

    def fake_runner(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        del repo_root, epoch, pass_fds
        assert strict is True
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        docs_dir = Path(parsed["docs_dir"])
        site_dir = Path(parsed["site_dir"])
        assert docs_dir.is_dir()
        assert (docs_dir / "index.md").is_file()
        assert docs_dir != (repo / "build/docs").resolve()
        assert site_dir.is_dir()
        seen["docs"] = docs_dir
        seen["site"] = site_dir
        (site_dir / "index.html").write_text("index", encoding="utf-8")
        (site_dir / "404.html").write_text("404", encoding="utf-8")
        return 0

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", fake_runner)
    result = build_site(
        repo,
        check=True,
        dry_run=True,
        strict=True,
        force=False,
        site_url=None,
    )

    assert result.published is False
    assert seen["docs"].exists() is False
    assert seen["site"].exists() is False
    assert not (repo / "build/docs").exists()
    assert not (repo / "site").exists()


def test_site_build_uses_live_docs_and_staged_site_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal MkDocs config points at published docs and a held site stage."""

    _mkdocs_source(repo)
    captured: dict[str, object] = {}

    def fake_runner(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        del repo_root, epoch, pass_fds
        assert strict is True
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        captured["config_path"] = config_path
        captured["parsed"] = parsed
        site_dir = Path(parsed["site_dir"])
        (site_dir / "index.html").write_text("index", encoding="utf-8")
        (site_dir / "404.html").write_text("404", encoding="utf-8")
        return 0

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", fake_runner)
    result = build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url=None,
    )

    parsed = captured["parsed"]
    assert isinstance(parsed, dict)
    assert parsed["docs_dir"] == str((repo / "build/docs").resolve())
    site_dir = Path(parsed["site_dir"])
    assert site_dir.as_posix().startswith(("/proc/self/fd/", "/dev/fd/"))
    assert not Path(captured["config_path"]).exists()
    assert result.published is True
    assert (repo / "site/index.html").is_file()
    assert (repo / "site/404.html").is_file()


def test_strict_site_failure_preserves_old_complete_site(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed strict runner cannot replace an existing complete site."""

    _mkdocs_source(repo)
    old = repo / "site"
    mark_generated_root(old, allowed_root=repo)
    (old / "keep.txt").write_text("old complete tree", encoding="utf-8")

    def failing_runner(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        del repo_root, epoch, pass_fds
        assert strict is True
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        (Path(parsed["site_dir"]) / "incomplete.txt").write_text("incomplete", encoding="utf-8")
        return 1

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", failing_runner)
    with pytest.raises(SiteBuildError, match="MkDocs"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"
    assert not list(repo.glob(".site.staging-*"))


def test_site_build_rejects_unmarked_existing_site(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmarked site is never treated as replaceable generated output."""

    _mkdocs_source(repo)
    site = repo / "site"
    site.mkdir()
    (site / "private.txt").write_text("private", encoding="utf-8")

    def valid_runner(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        del repo_root, strict, epoch, pass_fds
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        output = Path(parsed["site_dir"])
        (output / "index.html").write_text("index", encoding="utf-8")
        (output / "404.html").write_text("404", encoding="utf-8")
        return 0

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", valid_runner)

    with pytest.raises(SiteBuildError):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert (site / "private.txt").read_text(encoding="utf-8") == "private"


def test_fd_site_stage_survives_ancestor_swap(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MkDocs must keep writing through held FD stage after ancestor replacement."""

    _mkdocs_source(repo)
    parent = tmp_path / "held-parent"
    parent.mkdir()
    stage = parent / "stage"
    stage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    descriptor = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fd_stage = io_utils.fd_directory_path(descriptor)
        swapped = tmp_path / "held-parent-old"

        def runner(
            repo_root: Path,
            config_path: Path,
            *,
            strict: bool,
            epoch: int,
            pass_fds: tuple[int, ...],
        ) -> int:
            del repo_root, strict, epoch, pass_fds
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            configured_stage = Path(parsed["site_dir"])
            parent.rename(swapped)
            parent.symlink_to(outside, target_is_directory=True)
            (configured_stage / "index.html").write_text("index", encoding="utf-8")
            (configured_stage / "404.html").write_text("404", encoding="utf-8")
            return 0

        monkeypatch.setattr(build_site_module, "run_mkdocs_build", runner)
        hashes = build_site_module._run_site_build(
            repo_root=repo,
            docs_dir=repo / "content",
            site_dir=fd_stage,
            site_url=None,
            epoch=0,
        )

        assert "index.html" in hashes
        assert (swapped / "stage/index.html").read_text(encoding="utf-8") == "index"
        assert not (outside / "stage/index.html").exists()
    finally:
        os.close(descriptor)


def test_site_build_cleanup_error_does_not_replace_runner_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temporary-config cleanup failure is only an annotation on primary failure."""

    _mkdocs_source(repo)
    config_path = repo / "build/config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("site_name: Test\n", encoding="utf-8")
    monkeypatch.setattr(
        build_site_module,
        "write_temp_mkdocs_config",
        lambda **kwargs: config_path,
    )

    def fail_unlink(self: Path, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise OSError("unlink boom")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    def fail_runner(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise SiteBuildError("runner boom")

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", fail_runner)
    with pytest.raises(SiteBuildError, match="runner boom") as raised:
        build_site_module._run_site_build(
            repo_root=repo,
            docs_dir=repo / "content",
            site_dir=repo / "site-stage",
            site_url=None,
            epoch=0,
        )

    assert any("unlink boom" in note for note in raised.value.__notes__)


@pytest.mark.parametrize("raw", ("./index.md", "a//b.md", "index.md/"))
def test_cli_rejects_noncanonical_only_syntax(repo: Path, raw: str) -> None:
    """CLI validates raw --only spelling before PurePosixPath normalization."""

    assert main(["--repo-root", str(repo), "--dry-run", "--strict", "--only", raw]) == 2
    assert not (repo / "site").exists()
    assert not (repo / "build/docs").exists()


def test_mkdocs_runner_receives_default_source_date_epoch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess receives copied environment with deterministic default epoch."""

    _mkdocs_source(repo)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    captured: dict[str, str] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> SimpleNamespace:
        del cwd, check, capture_output, text
        captured.update(env)
        assert len(pass_fds) == 2
        config_path = Path(command[command.index("--config-file") + 1])
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        output = Path(parsed["site_dir"])
        (output / "index.html").write_text("index", encoding="utf-8")
        (output / "404.html").write_text("404", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("KEEP_ENV", "yes")
    monkeypatch.setattr(build_site_module.subprocess, "run", fake_run)
    build_site(
        repo,
        check=True,
        dry_run=True,
        strict=True,
        force=False,
        site_url=None,
    )

    assert captured["SOURCE_DATE_EPOCH"] == "0"
    assert captured["KEEP_ENV"] == "yes"


def test_cli_catches_invalid_source_date_epoch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid reproducibility input returns CLI failure before publication."""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-integer")
    assert main(["--repo-root", str(repo), "--check", "--strict"]) == 2
    assert not (repo / "site").exists()
    assert not (repo / "build/docs").exists()


def test_real_mkdocs_child_can_use_held_fd_stages(repo: Path) -> None:
    """A real MkDocs child can open both inherited FD-backed stage paths."""

    _mkdocs_source(repo)
    result = build_site(
        repo,
        check=True,
        dry_run=True,
        strict=True,
        force=False,
        site_url=None,
    )

    assert result.published is False
    assert not (repo / "site").exists()
    assert not (repo / "build/docs").exists()


def test_invalid_site_url_is_classified_as_site_build_error(repo: Path) -> None:
    """Malformed bracketed URL must not leak urllib ValueError through API or CLI."""

    with pytest.raises(SiteBuildError):
        build_site(
            repo,
            check=True,
            dry_run=True,
            strict=True,
            force=False,
            site_url="https://[invalid",
        )
    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--check",
                "--dry-run",
                "--strict",
                "--site-url",
                "https://[invalid",
            ]
        )
        == 2
    )
