"""Wikilink scanning, resolution, and conversion contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.web.content_index import build_content_index
from scripts.web.link_converters import convert_for_web
from scripts.web.link_resolution import resolve_occurrence
from scripts.web.link_types import LinkError, scan_wikilinks


def _write_page(
    root: Path,
    path: str,
    *,
    title: str,
    aliases: tuple[str, ...] = (),
    body: str = "",
) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    alias_yaml = ""
    if aliases:
        alias_yaml = "aliases:\n" + "".join(f'  - "{alias}"\n' for alias in aliases)
    target.write_text(
        f'---\ntitle: "{title}"\nrole: "axis"\n{alias_yaml}---\n\n{body}',
        encoding="utf-8",
    )
    return target


def _occurrence(source: Path, raw: str):
    found = scan_wikilinks(raw, source)
    assert len(found) == 1
    return found[0]


def test_scanner_ignores_frontmatter_code_comments_and_indented_code() -> None:
    text = (
        "---\ntitle: '[[frontmatter]]'\n---\n"
        "[[visible]]\n"
        "`[[inline]]`\n"
        "``start\n[[multiline]]\nend``\n"
        "```md\n[[fenced]]\n```\n"
        "~~~bad`info\n[[tilde]]\n~~~\n"
        "    [[indented]]\n"
        "<!-- [[commented]] -->\n"
    )
    found = scan_wikilinks(text, Path("page.md"))
    assert [item.target for item in found] == ["visible"]


def test_frontmatter_backticks_cannot_extend_code_context_into_body() -> None:
    text = "---\ntitle: '`trap [[hidden]]'\n---\n[[visible]]\nend`\n"
    assert [item.target for item in scan_wikilinks(text, Path("page.md"))] == ["visible"]


def test_multiline_html_comment_across_code_fence_hides_links_until_closed() -> None:
    text = (
        "<!-- comment starts\n```md\n[[hidden-in-fence]]\n```\n[[still-hidden]] -->\n[[visible]]\n"
    )
    assert [item.target for item in scan_wikilinks(text, Path("page.md"))] == ["visible"]


@pytest.mark.parametrize(
    "text",
    (
        "`prefix [[visible]] without close\n",
        "`prefix without close\n[[visible]]\n",
        "\\` escaped [[visible]]\n",
        "```bad`info\n[[visible]]\n```\n",
    ),
)
def test_unclosed_escaped_or_invalid_backticks_do_not_hide_links(text: str) -> None:
    assert [item.target for item in scan_wikilinks(text, Path("page.md"))] == ["visible"]


def test_many_invalid_fence_ranges_and_unclosed_runs_keep_exact_link_offsets() -> None:
    lines = [f"```bad`info [[target-{index}]]\n" for index in range(1000)]
    text = "".join(lines) + "prefix " + " ".join("`" * run for run in range(1, 101))

    found = scan_wikilinks(text, Path("page.md"))

    assert len(found) == 1000
    assert (found[0].target, found[0].line, found[0].column) == ("target-0", 1, 13)
    assert (found[-1].target, found[-1].line, found[-1].column) == (
        "target-999",
        1000,
        13,
    )


def test_scanner_preserves_offsets_for_multiple_links() -> None:
    text = "before [[one]] middle [[two|Two]] after"
    found = scan_wikilinks(text, Path("page.md"))
    assert [text[item.start : item.end] for item in found] == ["[[one]]", "[[two|Two]]"]
    assert [(item.line, item.column) for item in found] == [(1, 8), (1, 23)]


@pytest.mark.parametrize(
    ("text", "trailing"),
    (("[[target]]]", "]"), ("[[target]]]]]", "]]]")),
)
def test_scanner_closes_at_first_bracket_pair(text: str, trailing: str) -> None:
    occurrence = _occurrence(Path("page.md"), text)
    assert occurrence.target == "target"
    assert occurrence.raw == "[[target]]"
    assert text[occurrence.end :] == trailing


def test_lone_unicode_surrogate_in_wikilink_fails_closed() -> None:
    with pytest.raises(LinkError, match="Unicode surrogate"):
        scan_wikilinks("[[target|\ud800]]", Path("page.md"))


def test_resolution_supports_id_title_alias_paths_indexes_and_headings(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "nested/source.md", title="Source")
    target = _write_page(
        tmp_path,
        "guides/target.md",
        title="Target title",
        aliases=("Target alias",),
        body="## Remote heading {#remote}\n",
    )
    index_page = _write_page(tmp_path, "area/INDEX.md", title="Area")
    local = _write_page(tmp_path, "local.md", title="Local", body="## Local heading\n")
    index = build_content_index(tmp_path)
    target_id = index.page_for_path(target).page_id

    cases = {
        target_id: "guides/target.md",
        "Target title": "guides/target.md",
        "Target alias": "guides/target.md",
        "../guides/target.md": "guides/target.md",
        "../area": "area/INDEX.md",
        "../local#Local heading": "local.md",
        "../guides/target#remote": "guides/target.md",
    }
    for raw_target, expected in cases.items():
        resolution = resolve_occurrence(index, _occurrence(source, f"[[{raw_target}]]"))
        assert resolution.ok, resolution
        assert resolution.page.relative_path.as_posix() == expected
    assert resolve_occurrence(index, _occurrence(local, "[[#Local heading]]")).ok
    assert index.page_for_path(index_page).title == "Area"


@pytest.mark.parametrize(
    ("target", "status"),
    (
        ("../GUIDES/target.md", "case-mismatch"),
        ("../../outside.md", "root-escape"),
        ("/absolute.md", "root-escape"),
        ("https://example.invalid", "external"),
        ("missing", "missing-document"),
    ),
)
def test_resolution_fails_closed_for_case_escape_external_and_missing(
    tmp_path: Path, target: str, status: str
) -> None:
    source = _write_page(tmp_path, "nested/source.md", title="Source")
    _write_page(tmp_path, "guides/target.md", title="Target")
    resolution = resolve_occurrence(
        build_content_index(tmp_path), _occurrence(source, f"[[{target}]]")
    )
    assert resolution.status == status


def test_resolution_reports_ambiguous_lookup_and_heading(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "source.md", title="Source")
    _write_page(tmp_path, "a/topic.md", title="First", body="## Same\n## Same\n")
    _write_page(tmp_path, "b/topic.md", title="Second")
    index = build_content_index(tmp_path)
    assert resolve_occurrence(index, _occurrence(source, "[[topic]]")).status == "ambiguous"
    assert (
        resolve_occurrence(index, _occurrence(source, "[[a/topic#Same]]")).status
        == "ambiguous-heading"
    )


def test_assets_and_markdown_transclusion_are_handled_explicitly(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "source.md", title="Source")
    target = _write_page(tmp_path, "target.md", title="Target")
    (tmp_path / "image.png").write_bytes(b"png")
    (tmp_path / "manual.pdf").write_bytes(b"pdf")
    index = build_content_index(tmp_path)

    assert resolve_occurrence(index, _occurrence(source, "![[image.png]]")).ok
    assert resolve_occurrence(index, _occurrence(source, "[[manual.pdf]]")).ok
    assert (
        resolve_occurrence(index, _occurrence(source, "![[manual.pdf]]")).status
        == "unsupported-embed"
    )
    assert (
        resolve_occurrence(index, _occurrence(source, "![[target.md]]")).status
        == "markdown-transclusion"
    )
    assert index.page_for_path(target)


@pytest.mark.parametrize("suffix", (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp", ".pdf"))
def test_only_documented_local_asset_suffixes_resolve(tmp_path: Path, suffix: str) -> None:
    source = _write_page(tmp_path, "source.md", title="Source")
    (tmp_path / f"asset{suffix}").write_bytes(b"asset")

    resolution = resolve_occurrence(
        build_content_index(tmp_path),
        _occurrence(source, f"[[asset{suffix}]]"),
    )

    assert resolution.ok


@pytest.mark.parametrize("suffix", (".html", ".xhtml", ".js", ".mjs", ".svg", ".txt"))
def test_active_or_undocumented_asset_suffixes_fail_closed(tmp_path: Path, suffix: str) -> None:
    source = _write_page(tmp_path, "source.md", title="Source")
    (tmp_path / f"unsafe{suffix}").write_bytes(b"unsafe")

    resolution = resolve_occurrence(
        build_content_index(tmp_path),
        _occurrence(source, f"[[unsafe{suffix}]]"),
    )

    assert resolution.status == "missing-document"


def test_web_conversion_encodes_relative_paths_escapes_labels_and_replaces_in_reverse(
    tmp_path: Path,
) -> None:
    source = _write_page(
        tmp_path,
        "nested/source.md",
        title="Source",
        body="[[../Ziel ü.md|<b>A</b> & one]] and [[../Ziel ü.md#Heading|B [two]]]\n",
    )
    _write_page(tmp_path, "Ziel ü.md", title="Target", body="## Heading\n")
    index = build_content_index(tmp_path)
    text = source.read_text(encoding="utf-8")

    converted = convert_for_web(text, source, tmp_path, index=index)

    assert "[&lt;b&gt;A&lt;/b&gt; &amp; one](../Ziel%20%C3%BC.md)" in converted
    assert "[B \\[two\\]](../Ziel%20%C3%BC.md#heading)" in converted
    assert text.count("[[") == 2


def test_web_conversion_encodes_hash_in_path_separately_from_real_anchor(
    tmp_path: Path,
) -> None:
    source = _write_page(
        tmp_path,
        "source.md",
        title="Source",
        body="[[Hash target]] and [[Hash target#Heading]]\n",
    )
    _write_page(tmp_path, "Ziel #1.md", title="Hash target", body="## Heading\n")

    converted = convert_for_web(source.read_text(encoding="utf-8"), source, tmp_path)

    assert "[Hash target](Ziel%20%231.md)" in converted
    assert "[Heading](Ziel%20%231.md#heading)" in converted


def test_many_wikilinks_convert_with_exact_source_order(tmp_path: Path) -> None:
    body = "\n".join("[[Destination]]" for _ in range(1000)) + "\n"
    source = _write_page(tmp_path, "source.md", title="Source", body=body)
    _write_page(tmp_path, "target.md", title="Destination")
    source_text = source.read_text(encoding="utf-8")

    converted = convert_for_web(source_text, source, tmp_path)

    assert converted == source_text.replace("[[Destination]]", "[Destination](target.md)")


def test_conversion_fails_closed_for_unresolved_and_unsafe_links(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "source.md", title="Source", body="[[missing]]\n")
    with pytest.raises(LinkError, match="not found"):
        convert_for_web(source.read_text(encoding="utf-8"), source, tmp_path)
