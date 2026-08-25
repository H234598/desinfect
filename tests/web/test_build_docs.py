"""Atomic generated documentation build contracts."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path, PurePosixPath
import os
import shutil
from types import SimpleNamespace

import material
import mkdocs.config
import pytest
import yaml

from scripts.rki_pipeline import io_utils
from scripts.rki_pipeline.io_utils import mark_generated_root, stable_json_dumps
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
    shutil.copy2(source / "status.json", target / "status.json")
    return target


def _old_docs(repo: Path) -> Path:
    old = repo / "build/docs"
    mark_generated_root(old, allowed_root=repo)
    (old / "keep.txt").write_text("old complete tree", encoding="utf-8")
    return old


def _old_site(repo: Path) -> Path:
    old = repo / "site"
    mark_generated_root(old, allowed_root=repo)
    (old / "keep.txt").write_text("old complete tree", encoding="utf-8")
    return old


def _mkdocs_source(repo: Path) -> Path:
    """Return static MkDocs input required by site-build tests."""

    path = repo / "mkdocs.yml"
    assert path.is_file()
    return path


def _sha256_tree(root: Path, *, repo_root: Path) -> dict[str, str]:
    """Return regular-file hashes with sorted repository-relative POSIX keys."""

    return {
        path.relative_to(repo_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and not path.is_symlink()
    }


def _write_approved_table_state(repo: Path) -> None:
    """Write one small internally consistent approved table fixture."""

    status = yaml.safe_load((repo / "status.json").read_text(encoding="utf-8"))
    status["corpus"].update(
        inventory_complete_through_year=2020,
        analysis_corpus_complete_through_year=2020,
        taxonomy_gate_satisfied=True,
        taxonomy_state="approved",
    )
    status["periods"].update(
        last_completed_month="2020-12",
        last_completed_year=2020,
        last_reconciliation_at="2026-08-01T00:00:00Z",
    )
    (repo / "status.json").write_text(stable_json_dumps(status), encoding="utf-8")
    readiness = {
        "schema_version": "1.0.0",
        "policy_version": "1.0.0",
        "minimum_required_year": 2020,
        "inventory_complete_through_year": 2020,
        "analysis_corpus_complete_through_year": 2020,
        "public_mirror_complete_through_year": None,
        "monthly_archives_complete_through": "2020-12",
        "yearly_archives_complete_through_year": 2020,
        "unresolved_source_gaps": 0,
        "approved_source_exceptions": 1,
        "unresolved_conversion_failures": 0,
        "last_reconciliation_conclusion": "success",
        "taxonomy_gate_satisfied": True,
        "based_on_manifest_sha256": "a" * 64,
    }
    taxonomy = {
        "schema_version": "1.0.0",
        "version": "1.2.3",
        "status": "approved",
        "based_on_corpus_through_year": 2020,
        "based_on_manifest_sha256": "a" * 64,
        "categories": [
            {
                "id": "hand-hygiene",
                "label": "Handhygiene",
                "dimension": "area",
                "definition": "Synthetische Testkategorie.",
                "aliases": [],
                "evidence_count": 1,
                "source_examples": ["synthetic-test"],
                "review_status": "approved",
            }
        ],
        "migrations": [],
    }
    instruction = {
        "effectiveness_rank": 1,
        "application_area": "Hände",
        "title": "Serveranleitung",
        "active_ingredient": "Wirkstoff",
        "concentration": "70 %",
        "contact_time": "30 s",
        "spectrum": "synthetisch",
        "derived_categories": ["hand-hygiene"],
        "year": 2020,
        "temporal_status": "current",
        "confidence": "high",
        "bulletin": "Bulletin 1",
        "page": "1",
    }
    research = repo / "research"
    research.mkdir(exist_ok=True)
    (research / "corpus-readiness.json").write_text(stable_json_dumps(readiness), encoding="utf-8")
    (research / "taxonomy.yml").write_text(
        yaml.safe_dump(taxonomy, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    generated = repo / "content/generated-data"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "anleitungen.json").write_text(
        stable_json_dumps(
            {
                "schema_version": "1.0.0",
                "taxonomy_version": "1.2.3",
                "rows": [instruction],
            }
        ),
        encoding="utf-8",
    )


def test_external_asset_urls_are_case_and_whitespace_insensitive() -> None:
    """Remote resource schemes and link relations follow HTML casing rules."""

    html = """
    <script src="HTTPS://script.example/app.js"></script>
    <script src=" https://space.example/app.js"></script>
    <img src="//image.example/pixel.png">
    <source src="HTTP://media.example/movie.mp4">
    <iframe src=" https://frame.example/"></iframe>
    <link rel="STYLESHEET" href="https://style.example/site.css">
    <link rel="canonical" href="https://content.example/canonical">
    <a href="https://content.example/page">Content link</a>
    """

    assert build_site_module.external_asset_urls(html) == [
        "HTTPS://script.example/app.js",
        " https://space.example/app.js",
        "//image.example/pixel.png",
        "HTTP://media.example/movie.mp4",
        " https://frame.example/",
        "https://style.example/site.css",
    ]


def test_external_css_asset_urls_allow_local_and_data_targets() -> None:
    """CSS parser detects remote imports and URLs without rejecting local data."""

    css = """
    @import " HTTPS://style.example/remote.css";
    body { background: url( //image.example/background.png ); }
    .local { background: url('../images/local.png'); }
    .inline { background: url(data:image/svg+xml;base64,AAAA); }
    """

    assert build_site_module.external_css_asset_urls(css) == [
        "HTTPS://style.example/remote.css",
        "//image.example/background.png",
    ]


@pytest.mark.parametrize(
    "css",
    (
        r"body { background: u\72l(\68ttps://asset.example/escaped.png); }",
        r"body { background: \000075\000072\00006c(\000068ttps://asset.example/six.png); }",
        r"body { background: url(\68 ttps://asset.example/spaced.png); }",
        r"body { background: url(https\:\/\/asset.example/characters.png); }",
        "body { background: url(ht\\\ntps://asset.example/continued.png); }",
        r"@\69mport '\68ttps://asset.example/escaped.css';",
        "body { background: url(/* generated */ https://asset.example/comment.png); }",
        'body { background: url("https://asset.example/unclosed.png); }',
        'body { background: image-set("https://asset.example/set.png" 1x); }',
        'body { background: -webkit-image-set("//asset.example/set.png" 1x); }',
        r'body { background: \69mage-set(/* generated */ "\68ttps://asset.example/set.png" 1x); }',
        'body { background: image("https://asset.example/fallback.png"); }',
        r'body { background: \69mage(/* generated */ "\68ttps://asset.example/fallback.png"); }',
    ),
)
def test_external_css_asset_urls_decode_browser_syntax(css: str) -> None:
    """Escapes, comments, and ambiguous remote targets cannot hide fetches."""

    assert build_site_module.external_css_asset_urls(css)


def test_external_css_asset_urls_allow_escaped_local_and_commented_data_targets() -> None:
    """CSS normalization does not turn local or data targets into remote assets."""

    css = r"""
    .local { background: u\72l('../images/local.png'); }
    .inline { background: url(/* generated */ data:image/svg+xml;base64,AAAA); }
    .set { background: image-set('/local.png' 1x, "data:image/png;base64,AAAA" 2x); }
    .vendor { background: -webkit-image-set("#local" 1x); }
    .fallback { background: image("/local.png"); }
    """

    assert build_site_module.external_css_asset_urls(css) == []


@pytest.mark.parametrize(
    "css",
    (
        "x::before { content: \"image-set('https://asset.example/literal.png' 1x)\"; }",
        "[data-image=\"image('https://asset.example/literal.png')\"] { color: red; }",
        "x { --literal: \"-webkit-image-set('//asset.example/literal.png' 1x)\"; }",
        r"""x::before { content: "\\\" image-set('https://asset.example/literal.png' 1x)"; }""",
        r"""[data-image="\\\" image('https://asset.example/literal.png')"] { color: red; }""",
    ),
)
def test_external_css_asset_urls_ignore_image_functions_in_strings(css: str) -> None:
    """Image-function text inside a CSS string does not load a resource."""

    assert build_site_module.external_css_asset_urls(css) == []


def test_css_image_function_scan_has_linear_character_reads() -> None:
    """Nested image functions cannot trigger a rescan from every opening token."""

    reads = [0]

    class CountingCss(str):
        def __getitem__(self, key: int | slice) -> str:
            reads[0] += 1
            return super().__getitem__(key)

    target = "https://asset.example/nested.png"
    nesting = 200
    css = CountingCss("image(" * nesting + repr(target) + ")" * nesting)

    assert [value for _, value in build_site_module._css_image_function_strings(css)] == [target]
    assert reads[0] <= len(css) * 2


@pytest.mark.parametrize(
    "html",
    (
        '<script src="\x01 h\tttps://asset.example/app.js"></script>',
        '<img srcset="/local.png 1x, https://asset.example/image.png 2x">',
        '<source srcset="/local.webp 1x, //asset.example/image.webp 2x">',
        '<link rel="preload" href="/local.png" '
        'imagesrcset="/local.png 1x, HTTP://asset.example/image.png 2x">',
        '<video src="https://asset.example/movie.mp4"></video>',
        '<video poster="//asset.example/poster.png"></video>',
        '<audio src="https://asset.example/audio.mp3"></audio>',
        '<track src="https://asset.example/captions.vtt">',
        '<embed src="https://asset.example/plugin.bin">',
        '<object data="https://asset.example/object.bin"></object>',
        '<base href="https://asset.example/root/">',
        r'<base href="\\evil.example/root/">',
        r'<img src="\\evil.example/image.png">',
        r'<img srcset="/local.png 1x, \\evil.example/image.png 2x">',
        '<svg><image href="https://asset.example/image.svg"></image></svg>',
        '<svg><use xlink:href="//asset.example/icons.svg#x"></use></svg>',
        '<svg><feImage href="https://asset.example/filter.svg"></feImage></svg>',
        '<svg><script href="https://asset.example/app.js"></script></svg>',
        '<svg><script xlink:href="//asset.example/app.js"></script></svg>',
        '<svg><linearGradient href="https://asset.example/paint.svg#g"></linearGradient></svg>',
        '<svg><radialGradient xlink:href="//asset.example/paint.svg#g"></radialGradient></svg>',
        '<svg><pattern href="https://asset.example/pattern.svg#p"></pattern></svg>',
        '<svg><textPath xlink:href="//asset.example/text.svg#p"></textPath></svg>',
        '<svg><mpath href="https://asset.example/motion.svg#p"></mpath></svg>',
        '<svg><rect fill="url(https://asset.example/paint.svg#g)"></rect></svg>',
        '<svg><path stroke="url(//asset.example/paint.svg#g)"></path></svg>',
        '<svg><g filter="url(https://asset.example/filter.svg#f)"></g></svg>',
        '<svg><g mask="url(//asset.example/mask.svg#m)"></g></svg>',
        '<svg><g clip-path="url(https://asset.example/clip.svg#c)"></g></svg>',
        '<svg><path marker="url(//asset.example/marker.svg#m)"></path></svg>',
        '<svg><path marker-start="url(https://asset.example/marker.svg#m)"></path></svg>',
        '<svg><path marker-mid="url(//asset.example/marker.svg#m)"></path></svg>',
        '<svg><path marker-end="url(https://asset.example/marker.svg#m)"></path></svg>',
        '<input type="image" src="https://asset.example/button.png">',
        '<div style="background: url(https://asset.example/inline.png)"></div>',
        "<div style=\"background: image-set('https://asset.example/set.png' 1x)\"></div>",
        "<div style=\"background: image('https://asset.example/fallback.png')\"></div>",
        "<style>body { background: url(//asset.example/block.png); }</style>",
        '<style>body { background: -webkit-image-set("//asset.example/set.png" 1x); }</style>',
        "<style>body { background: url(https://asset.example/unclosed.png); }",
        "<iframe srcdoc=\"&lt;img src='https://asset.example/nested.png'>\"></iframe>",
    ),
)
def test_external_asset_urls_cover_browser_resource_carriers(html: str) -> None:
    """Every browser-loading attribute or embedded CSS form is rejected."""

    assert build_site_module.external_asset_urls(html)


def test_external_asset_urls_allow_navigation_and_local_resources() -> None:
    """Navigation links plus local, fragment, and data resources stay valid."""

    html = """
    <a href="https://content.example/page">Content link</a>
    <link rel="canonical" href="https://content.example/canonical">
    <base href="/local/root/">
    <base href="\\local/root/">
    <img src="/local.png" srcset="/local.png 1x, data:image/png;base64,AAAA 2x">
    <source src="#local" srcset="data:text/plain,https://not-loaded.example 1x">
    <video src="data:video/mp4;base64,AAAA" poster="/poster.png"></video>
    <object data="#local-object"></object>
    <svg><use href="#local-icon"></use></svg>
    <svg><script href="/local-script.js"></script></svg>
    <svg><linearGradient href="#local-gradient"></linearGradient></svg>
    <svg><rect fill="url(#local-gradient)" marker-end="url(#local-marker)"></rect></svg>
    <input type="image" src="data:image/png;base64,AAAA">
    <div style="background: url(data:image/png;base64,AAAA)"></div>
    <div style="background: image-set('/local.png' 1x, 'data:image/png;base64,AAAA' 2x)"></div>
    <style>body { background: url('/local.png'); }</style>
    <iframe srcdoc="&lt;a href='https://content.example/nested'>allowed&lt;/a>"></iframe>
    """

    assert build_site_module.external_asset_urls(html) == []


def test_build_docs_publishes_transformed_copy_and_preserves_sources(repo: Path) -> None:
    """Missing conversion, sentinel, or source purity makes this fail."""

    before = snapshot_sources(repo)
    result = build_docs(repo, force=False)
    rendered = (repo / "build/docs/index.md").read_text(encoding="utf-8")

    assert "[Händedesinfektion](Handdesinfektion.md){ .landing-card }" in rendered
    assert "[[" not in rendered
    assert result.published is True
    assert snapshot_sources(repo) == before
    assert (repo / "build/docs/.desinfect-generated").is_file()


def test_build_docs_renders_no_js_corpus_empty_state_and_public_projection(repo: Path) -> None:
    """Leaving the table marker or omitting server data makes this fail."""

    build_docs(repo, force=False)

    rendered = (repo / "build/docs/Tabelle.md").read_text(encoding="utf-8")
    projection = repo / "build/docs/assets/data/corpus-table.json"
    assert "<!-- DESINFECT_TABLE -->" not in rendered
    assert "Noch keine validierten Dokumentmanifeste" in rendered
    assert "Rechtezustand" in rendered
    assert "Quelle" in rendered
    assert '<table data-enhance-table="corpus">' in rendered
    assert projection.is_file()
    assert yaml.safe_load(projection.read_text(encoding="utf-8")) == {
        "schema_version": "1.0.0",
        "rows": [],
    }


def test_build_docs_renders_corpus_rows_without_copying_raw_projection(repo: Path) -> None:
    """Client-only data or leaked generated-data sources make this fail."""

    raw = {
        "schema_version": "1.0.0",
        "rows": [
            {
                "document_type": "bulletin",
                "title": "Server < row",
                "year": 2020,
                "month": 12,
                "rki_handle": "176904/1234",
                "doi": None,
                "rights_state": "metadata_only",
                "pdf_present": False,
                "markdown_status": "validated",
                "ocr_status": "not_required",
                "monthly_archive_present": True,
                "yearly_archive_present": True,
                "checksum": "b" * 64,
                "source": "https://edoc.rki.de/handle/176904/1234",
            }
        ],
    }
    source = repo / "content/generated-data/corpus-table.json"
    source.parent.mkdir(parents=True)
    source.write_text(stable_json_dumps(raw), encoding="utf-8")

    build_docs(repo, force=False)

    rendered = (repo / "build/docs/Tabelle.md").read_text(encoding="utf-8")
    assert "Server &lt; row" in rendered
    assert not (repo / "build/docs/generated-data/corpus-table.json").exists()
    assert (repo / "build/docs/assets/data/corpus-table.json").is_file()


def test_partial_build_without_table_does_not_load_or_project_table_inputs(repo: Path) -> None:
    """Loading unrelated table state during a partial preview makes this fail."""

    (repo / "status.json").write_text("not json", encoding="utf-8")
    stage = repo / "stage"
    stage.mkdir()

    render_docs_tree(repo, stage, only=(PurePosixPath("index.md"),))

    assert (stage / "index.md").is_file()
    assert not (stage / "assets/data/corpus-table.json").exists()


def test_approved_table_build_renders_both_tables_in_strict_html(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping approved server rows before MkDocs or the public asset makes this fail."""

    _write_approved_table_state(repo)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url="https://h234598.github.io/desinfect/",
    )

    html = (repo / "site/Tabelle/index.html").read_text(encoding="utf-8")
    assert "Korpustabelle" in html
    assert "Anleitungstabelle" in html
    assert "Serveranleitung" in html
    assert '<table data-enhance-table="instructions">' in html
    assert (repo / "build/docs/assets/data/anleitungen.json").is_file()
    assert not (repo / "build/docs/generated-data/anleitungen.json").exists()


@pytest.mark.parametrize(
    "relative",
    (
        "status.json",
        "research/corpus-readiness.json",
        "research/taxonomy.yml",
    ),
)
def test_table_state_source_drift_aborts_and_preserves_old_docs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    """Leaving any gate source outside the before/after snapshot makes this fail."""

    _write_approved_table_state(repo)
    old = _old_docs(repo)
    target = repo / relative
    real_load = build_docs_module.load_table_inputs

    def load_then_mutate(root: Path) -> object:
        result = real_load(root)
        target.write_bytes(target.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(build_docs_module, "load_table_inputs", load_then_mutate)

    with pytest.raises(BuildDocsError, match="source-hash drift"):
        build_docs(repo, force=False)

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


@pytest.mark.parametrize(
    "relative",
    (
        "status.json",
        "research/corpus-readiness.json",
        "research/taxonomy.yml",
    ),
)
def test_snapshot_sources_rejects_symlinked_table_state(repo: Path, relative: str) -> None:
    """Following an exact gate-input symlink makes this fail."""

    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    target.symlink_to(repo / "content/index.md")

    with pytest.raises(BuildDocsError, match="source file"):
        snapshot_sources(repo)


def test_invalid_table_keeps_old_docs_and_site(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacing either published tree before table validation makes this fail."""

    old_docs = _old_docs(repo)
    old_site = _old_site(repo)
    generated = repo / "content/generated-data"
    generated.mkdir(parents=True)
    (generated / "corpus-table.json").write_text(
        stable_json_dumps({"schema_version": "1.0.0", "rows": [], "extra": True}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    with pytest.raises(SiteBuildError, match="corpus root keys"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert (old_docs / "keep.txt").read_text(encoding="utf-8") == "old complete tree"
    assert (old_site / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


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
    assert str(parsed["docs_dir"]).startswith(("/proc/self/fd/", "/dev/fd/"))
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
            partial=False,
        )

        assert "index.html" in hashes
        assert (swapped / "stage/index.html").read_text(encoding="utf-8") == "index"
        assert not (outside / "stage/index.html").exists()
    finally:
        os.close(descriptor)


def test_normal_site_build_holds_docs_and_config_across_build_swap(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real child reads held docs/config and writes held site after build/ replacement."""

    old = _old_site(repo)
    build_root = repo / "build"
    moved_build = repo / "build-held"
    outside = repo.parent / "outside-build"
    outside.mkdir()
    real_runner = build_site_module.run_mkdocs_build
    swapped = False
    returncodes: list[int] = []

    def runner_after_swap(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        nonlocal swapped
        build_root.rename(moved_build)
        build_root.symlink_to(outside, target_is_directory=True)
        swapped = True
        returncode = real_runner(
            repo_root,
            config_path,
            strict=strict,
            epoch=epoch,
            pass_fds=pass_fds,
        )
        returncodes.append(returncode)
        return returncode

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", runner_after_swap)
    with pytest.raises(SiteBuildError, match="unsafe publication path"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert swapped is True
    assert returncodes == [0]
    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"
    assert (moved_build / "docs/index.md").is_file()
    assert not list(moved_build.rglob(".mkdocs-build-*.yml"))
    assert not list((moved_build / ".mkdocs-config-preview").iterdir())
    assert not list(outside.iterdir())


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
            partial=False,
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
        assert len(pass_fds) == 3
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


def test_navigation_validation_uses_configured_content_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Static navigation validates targets from publication content_root."""

    configured_content = repo / "published-content"
    (repo / "content").rename(configured_content)
    (repo / "content").mkdir()
    config_path = repo / "config/publication.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "content_root: content", "content_root: published-content"
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    result = build_site(
        repo,
        check=True,
        dry_run=True,
        strict=True,
        force=False,
        site_url=None,
    )

    assert result.published is False


def test_strict_site_is_local_german_and_has_404(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong locale, remote assets, or missing local 404 output breaks full build."""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    result = build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url="https://h234598.github.io/desinfect/",
    )

    config = mkdocs.config.load_config(
        config_file=str(repo / "mkdocs.yml"), docs_dir=str(repo / "content")
    )
    theme = config["theme"]
    index_html = (repo / "site/index.html").read_text(encoding="utf-8")
    not_found = (repo / "site/404.html").read_text(encoding="utf-8")
    stylesheet = (repo / "site/assets/stylesheets/extra.css").read_text(encoding="utf-8")

    assert result.published is True
    assert config["site_name"] == "Desinfect"
    assert theme.name == "material"
    assert theme["language"] == "de"
    assert theme["font"] is False
    assert config["plugins"]["material/search"].config["lang"] == ["de"]
    assert {entry["media"] for entry in theme["palette"]} == {
        "(prefers-color-scheme: light)",
        "(prefers-color-scheme: dark)",
    }
    assert config["extra_css"] == ["assets/stylesheets/extra.css"]
    assert Path(material.__file__).is_file()
    assert "Desinfect" in index_html
    assert "404" in not_found
    assert "Seite nicht gefunden" in not_found
    assert '<a href="/desinfect/">Zur Startseite</a>' in not_found
    assert not build_site_module.external_asset_urls(index_html + not_found)
    assert all(not path.is_symlink() for path in (repo / "site").rglob("*"))
    assert "@import" not in stylesheet.lower()
    assert "url(" not in stylesheet.lower()
    assert "@font-face" not in stylesheet.lower()


def test_full_site_build_is_reproducible_and_preserves_sources(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wall-clock output or source mutation makes identical full builds diverge."""

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    before = snapshot_sources(repo)

    first = build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url="https://h234598.github.io/desinfect/",
    )
    first_hashes = _sha256_tree(repo / "site", repo_root=repo)
    assert (repo / "site/index.html").is_file()
    assert (repo / "site/404.html").is_file()

    second = build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=True,
        site_url="https://h234598.github.io/desinfect/",
    )
    second_hashes = _sha256_tree(repo / "site", repo_root=repo)

    assert (repo / "site/index.html").is_file()
    assert (repo / "site/404.html").is_file()
    assert first.published is True
    assert second.published is True
    assert first_hashes == second_hashes
    assert snapshot_sources(repo) == before


def test_temp_mkdocs_config_is_reproducible_for_equivalent_key_order(repo: Path) -> None:
    """Equivalent YAML key order must not alter generated configuration bytes."""

    docs_dir = repo / "content"
    site_dir = repo / "site-stage"
    site_dir.mkdir()
    source = _mkdocs_source(repo)
    config_dir = repo / "config-stage"
    config_dir.mkdir()
    descriptor = os.open(config_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        held_config_dir = io_utils.fd_directory_path(descriptor)
        first_path = build_site_module.write_temp_mkdocs_config(
            repo_root=repo,
            content_root=repo / "content",
            config_dir=held_config_dir,
            docs_dir=docs_dir,
            site_dir=site_dir,
            site_url="https://h234598.github.io/desinfect/",
            source_date_epoch=1700000000,
            partial=False,
        )
        try:
            first = first_path.read_bytes()
        finally:
            first_path.unlink()

        parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
        source.write_text(
            yaml.safe_dump(
                {key: parsed[key] for key in reversed(tuple(parsed))},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        second_path = build_site_module.write_temp_mkdocs_config(
            repo_root=repo,
            content_root=repo / "content",
            config_dir=held_config_dir,
            docs_dir=docs_dir,
            site_dir=site_dir,
            site_url="https://h234598.github.io/desinfect/",
            source_date_epoch=1700000000,
            partial=False,
        )
        try:
            second = second_path.read_bytes()
        finally:
            second_path.unlink()
    finally:
        os.close(descriptor)

    assert first == second


def test_invalid_epoch_preserves_old_complete_site(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid reproducibility input must fail before replacing marked output."""

    old = _old_site(repo)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-epoch")

    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=True,
            site_url="https://h234598.github.io/desinfect/",
        )

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


def test_external_font_config_fails_before_runner_and_preserves_old_site(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured font download must fail closed before MkDocs can publish."""

    old = _old_site(repo)
    config_path = repo / "mkdocs.yml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("font: false", "font:\n    text: Roboto"),
        encoding="utf-8",
    )

    def unexpected_runner(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise AssertionError("MkDocs runner must not execute")

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", unexpected_runner)
    with pytest.raises(SiteBuildError, match="font"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        (
            "nested/details.html",
            '<a href="https://content.example/page">allowed</a>'
            '<IMG SRC=" HTTPS://asset.example/tracker.png">',
        ),
        ("assets/remote-import.css", '@IMPORT " HTTPS://asset.example/theme.css";'),
        ("assets/remote-url.css", "body { background: URL( //asset.example/image.png ); }"),
        ("control.html", '<script src="ht\ntps://asset.example/app.js"></script>'),
        (
            "srcset.html",
            '<img src="/local.png" srcset="/local.png 1x, h\tttps://asset.example/image.png 2x">',
        ),
        (
            "inline-style.html",
            '<div style="background: url(https://asset.example/inline.png)"></div>',
        ),
        ("base.html", '<base href=" //asset.example/root/">'),
        ("media.html", '<video poster="https://asset.example/poster.png"></video>'),
        ("object.html", '<object data="HTTP://asset.example/object.bin"></object>'),
        (
            "svg-script.html",
            '<svg><script xlink:href="https://asset.example/app.js"></script></svg>',
        ),
        ("input.html", '<input type="image" src="//asset.example/button.png">'),
        (
            "unclosed-style.html",
            "<style>body { background: url(https://asset.example/unclosed.png); }",
        ),
        (
            "assets/escaped.css",
            r"body { background: u\72l(\68ttps://asset.example/escaped.png); }",
        ),
        (
            "assets/comment.css",
            "body { background: url(/* generated */ https://asset.example/comment.png); }",
        ),
        ("backslash.html", r'<base href="\\evil.example/root/">'),
        (
            "assets/image-set.css",
            'body { background: image-set("https://asset.example/set.png" 1x); }',
        ),
        (
            "assets/image.css",
            'body { background: image("https://asset.example/fallback.png"); }',
        ),
        (
            "svg-href.html",
            '<svg><pattern xlink:href="https://asset.example/pattern.svg#p"></pattern></svg>',
        ),
        (
            "svg-presentation.html",
            '<svg><rect fill="url(https://asset.example/paint.svg#g)"></rect></svg>',
        ),
    ),
)
def test_external_generated_resource_preserves_old_site(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: str,
) -> None:
    """Remote resources anywhere in generated HTML or CSS abort publication."""

    old = _old_site(repo)
    before = _sha256_tree(old, repo_root=repo)

    def runner_with_remote_resource(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        del repo_root, strict, epoch, pass_fds
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        stage = Path(parsed["site_dir"])
        (stage / "index.html").write_text("index", encoding="utf-8")
        (stage / "404.html").write_text("404", encoding="utf-8")
        mutation = stage / relative_path
        mutation.parent.mkdir(parents=True, exist_ok=True)
        mutation.write_text(content, encoding="utf-8")
        return 0

    monkeypatch.setattr(
        build_site_module,
        "run_mkdocs_build",
        runner_with_remote_resource,
    )
    with pytest.raises(SiteBuildError, match="external browser resource"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert _sha256_tree(old, repo_root=repo) == before


def test_post_build_symlink_preserves_old_site(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink inserted after real MkDocs output must abort site replacement."""

    old = _old_site(repo)
    real_runner = build_site_module.run_mkdocs_build
    real_returncodes: list[int] = []

    def runner_with_symlink(
        repo_root: Path,
        config_path: Path,
        *,
        strict: bool,
        epoch: int,
        pass_fds: tuple[int, ...],
    ) -> int:
        returncode = real_runner(
            repo_root,
            config_path,
            strict=strict,
            epoch=epoch,
            pass_fds=pass_fds,
        )
        real_returncodes.append(returncode)
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        (Path(parsed["site_dir"]) / "escape").symlink_to(repo / "content/index.md")
        return returncode

    monkeypatch.setattr(build_site_module, "run_mkdocs_build", runner_with_symlink)
    with pytest.raises(SiteBuildError, match="cannot validate generated site"):
        build_site(
            repo,
            check=True,
            dry_run=False,
            strict=True,
            force=False,
            site_url=None,
        )

    assert real_returncodes == [0]
    assert (old / "keep.txt").read_text(encoding="utf-8") == "old complete tree"


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
