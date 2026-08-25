"""Read-only content index and heading contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.web.content_index import build_content_index, parse_headings
from scripts.web.content_model import ContentModelError, canonical_url_from_path


def _write_page(
    root: Path,
    path: str,
    *,
    title: str,
    role: str = "axis",
    aliases: tuple[str, ...] = (),
    body: str = "",
) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    alias_yaml = ""
    if aliases:
        alias_yaml = "aliases:\n" + "".join(f'  - "{alias}"\n' for alias in aliases)
    target.write_text(
        f'---\ntitle: "{title}"\nrole: "{role}"\n{alias_yaml}---\n\n{body}',
        encoding="utf-8",
    )
    return target


def test_repository_has_five_minimal_pages_without_taxonomy_role() -> None:
    root = Path(__file__).resolve().parents[2] / "content"
    index = build_content_index(root)
    assert {page.relative_path.as_posix() for page in index.pages} == {
        "index.md",
        "Handdesinfektion.md",
        "Flaechendesinfektion.md",
        "Kategorien.md",
        "WARTUNG.md",
    }
    assert index.page_for_path("Kategorien.md").role == "method"
    assert not any(page.role in {"taxonomy", "category"} for page in index.pages)


def test_index_derives_ids_urls_and_lookup_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "index.md", title="Home", role="landing", aliases=("Start",))
    _write_page(tmp_path, "guide.md", title="Guide")
    expected = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def reject_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("content index must not write")

    monkeypatch.setattr(Path, "write_bytes", reject_write)
    monkeypatch.setattr(Path, "write_text", reject_write)
    monkeypatch.setattr(Path, "mkdir", reject_write)
    monkeypatch.setattr(os, "replace", reject_write)
    monkeypatch.setattr(os, "remove", reject_write)
    index = build_content_index(tmp_path)

    assert index.page_for_path("index.md").canonical_url == "/"
    assert index.lookup_pages("Start") == (index.page_for_path("index.md"),)
    assert index.lookup_pages(index.page_for_path("guide.md").page_id)
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == expected


@pytest.mark.parametrize(
    ("paths", "message"),
    (
        (("Page.md", "page.md"), "portable content path collision"),
        (("topic.md", "topic/index.md"), "duplicate canonical URL"),
    ),
)
def test_index_rejects_portable_path_and_url_collisions(
    tmp_path: Path, paths: tuple[str, str], message: str
) -> None:
    _write_page(tmp_path, paths[0], title="First")
    _write_page(tmp_path, paths[1], title="Second")
    with pytest.raises(ContentModelError, match=message):
        build_content_index(tmp_path)


@pytest.mark.parametrize(
    ("first_aliases", "second_title", "second_aliases"),
    (
        ((), "Same", ()),
        (("Shared",), "Second", ("Other", "Shared")),
        (("First",), "Second", ()),
    ),
)
def test_index_rejects_duplicate_titles_and_aliases(
    tmp_path: Path,
    first_aliases: tuple[str, ...],
    second_title: str,
    second_aliases: tuple[str, ...],
) -> None:
    _write_page(
        tmp_path,
        "first.md",
        title="First" if second_title != "Same" else "Same",
        aliases=first_aliases,
    )
    _write_page(tmp_path, "second.md", title=second_title, aliases=second_aliases)
    with pytest.raises(ContentModelError, match="duplicate title or alias"):
        build_content_index(tmp_path)


def test_headings_ignore_protected_regions_and_assign_deterministic_anchors(tmp_path: Path) -> None:
    body = (
        "# Visible\n"
        "## Repeated\n"
        "## Repeated\n"
        "## Explicit {#fixed}\n"
        "```md\n# Fenced\n```\n"
        "    # Indented\n"
        "<!--\n# Commented\n-->\n"
        "`inline\n# Hidden inline\nend`\n"
    )
    _write_page(tmp_path, "page.md", title="Page", body=body)
    page = build_content_index(tmp_path).page_for_path("page.md")
    assert [(heading.text, heading.anchor, heading.explicit) for heading in page.headings] == [
        ("Visible", "visible", False),
        ("Repeated", "repeated", False),
        ("Repeated", "repeated_1", False),
        ("Explicit", "fixed", True),
    ]


def test_heading_text_preserves_inline_code_and_normalizes_hidden_comment_space() -> None:
    body = (
        "## Use `safe_api` now\n"
        "## Keep `*literal_value*` markers\n"
        "## Before <!-- hidden --> After\n"
        "```md\n## Fenced\n```\n"
        "    ## Indented\n"
        "<!--\n## Commented\n-->\n"
    )
    original = body.encode()

    headings = parse_headings(body, 1)

    assert [(heading.text, heading.anchor) for heading in headings] == [
        ("Use safe_api now", "use-safe-api-now"),
        ("Keep *literal_value* markers", "keep-literal-value-markers"),
        ("Before After", "before-after"),
    ]
    assert body.encode() == original


def test_index_url_preserves_dot_prefixed_directory() -> None:
    assert canonical_url_from_path(".archiv/index.md", "axis") == "/.archiv/"
    assert canonical_url_from_path("index.md", "axis") == "/"


def test_duplicate_explicit_heading_anchor_is_rejected(tmp_path: Path) -> None:
    _write_page(tmp_path, "page.md", title="Page", body="## One {#same}\n## Two {#same}\n")
    with pytest.raises(ContentModelError, match="duplicate explicit heading anchor"):
        build_content_index(tmp_path)


def test_lfs_pointer_is_never_parsed_as_markdown_page(tmp_path: Path) -> None:
    (tmp_path / "pointer.md").write_text(
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{'a' * 64}\nsize 123\n",
        encoding="utf-8",
    )
    with pytest.raises(ContentModelError, match="Git LFS pointer"):
        build_content_index(tmp_path)
