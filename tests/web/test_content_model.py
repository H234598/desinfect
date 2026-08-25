"""Hardened page and frontmatter contracts for P10.1."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib

import pytest

from scripts.web.content_model import (
    PAGE_ROLES,
    ContentModelError,
    ContentPage,
    advance_fence_state,
    page_id_from_path,
)


def _source(
    title: str = "Page",
    role: str = "axis",
    body: str = "",
    *,
    extra: str = "",
) -> str:
    return f'---\ntitle: "{title}"\nrole: "{role}"\n{extra}---\n\n{body}'


def test_roles_are_exact_and_path_identity_is_stable() -> None:
    assert PAGE_ROLES == frozenset(
        {
            "landing",
            "axis",
            "bulletin",
            "instruction",
            "method",
            "maintenance",
            "generated-wrapper",
        }
    )
    path = "Überblick.md"
    digest = hashlib.sha256("desinfect-page-v1\0Überblick.md".encode("utf-8")).hexdigest()
    expected = f"p_{digest[:16]}"
    assert len(expected.removeprefix("p_")) == 16
    assert page_id_from_path(path) == expected
    first = ContentPage.from_markdown(path, _source())
    second = ContentPage.from_markdown(path, _source(title="Changed"))
    assert first.page_id == second.page_id == expected


@pytest.mark.parametrize("role", sorted(PAGE_ROLES))
def test_every_declared_role_is_accepted(role: str) -> None:
    assert ContentPage.from_markdown("page.md", _source(role=role)).role == role


@pytest.mark.parametrize("role", ("", "taxonomy", "category", "Axis", "unknown"))
def test_unknown_or_noncanonical_roles_fail_closed(role: str) -> None:
    with pytest.raises(ContentModelError, match="role"):
        ContentPage.from_markdown("page.md", _source(role=role))


@pytest.mark.parametrize("missing", ("title", "role"))
def test_required_frontmatter_fields_are_strict(missing: str) -> None:
    lines = {"title": 'title: "Page"\n', "role": 'role: "axis"\n'}
    lines[missing] = ""
    source = f"---\n{lines['title']}{lines['role']}---\n"
    with pytest.raises(ContentModelError, match=missing):
        ContentPage.from_markdown("page.md", source)


def test_frontmatter_identity_cannot_be_selected_freely() -> None:
    with pytest.raises(ContentModelError, match="derived from content path"):
        ContentPage.from_markdown("page.md", _source(extra='id: "chosen"\n'))


def test_safe_existing_rki_metadata_is_preserved_and_immutable() -> None:
    source = _source(
        "Bulletin",
        "bulletin",
        "Quelle.\n",
        extra=(
            'aliases: ["Ausgabe 12"]\n'
            'document_type: "gesamtausgabe"\n'
            'source_url: "https://edoc.rki.de/handle/176904/12345"\n'
            'source_pdf: "rki/Bulletins/Jahre/1996/PDF/source.pdf"\n'
            "year: 1996\n"
            "ocr_used: false\n"
        ),
    )
    page = ContentPage.from_markdown("Bulletins/1996-12.md", source)
    assert page.aliases == ("Ausgabe 12",)
    assert page.metadata["document_type"] == "gesamtausgabe"
    assert page.source == source
    with pytest.raises(FrozenInstanceError):
        page.title = "changed"
    with pytest.raises(TypeError):
        page.metadata["year"] = 2000


@pytest.mark.parametrize("duplicate", ("title", "role", "source_url"))
def test_duplicate_yaml_keys_are_rejected_at_every_level(duplicate: str) -> None:
    values = {"title": "Other", "role": "method", "source_url": "https://other.invalid"}
    extra = 'source_url: "https://example.invalid"\n' + f'{duplicate}: "{values[duplicate]}"\n'
    with pytest.raises(ContentModelError, match="duplicate frontmatter key"):
        ContentPage.from_markdown("page.md", _source(extra=extra))

    nested = _source(extra="meta:\n  key: one\n  key: two\n")
    with pytest.raises(ContentModelError, match="duplicate frontmatter key"):
        ContentPage.from_markdown("page.md", nested)


@pytest.mark.parametrize(
    "extra",
    ("meta: &recursive [*recursive]\n", "first: &shared [one]\nsecond: *shared\n"),
)
def test_yaml_aliases_and_recursive_graphs_are_rejected(extra: str) -> None:
    with pytest.raises(ContentModelError, match="YAML aliases are not allowed"):
        ContentPage.from_markdown("page.md", _source(extra=extra))


def test_yaml_depth_and_node_budgets_accept_boundary_and_reject_excess() -> None:
    allowed_depth = "[" * 14 + '"leaf"' + "]" * 14
    denied_depth = "[" * 15 + '"leaf"' + "]" * 15
    assert ContentPage.from_markdown(
        "allowed.md", _source(extra=f"meta: {allowed_depth}\n")
    ).metadata["meta"]
    with pytest.raises(
        ContentModelError,
        match="depth budget of 16 nesting levels",
    ) as exc_info:
        ContentPage.from_markdown("denied.md", _source(extra=f"meta: {denied_depth}\n"))
    assert "YAML nodes" not in str(exc_info.value)

    allowed_nodes = ", ".join("0" for _ in range(1017))
    page = ContentPage.from_markdown("allowed.md", _source(extra=f"meta: [{allowed_nodes}]\n"))
    assert len(page.metadata["meta"]) == 1017
    with pytest.raises(ContentModelError, match="node budget of 1024"):
        ContentPage.from_markdown("denied.md", _source(extra=f"meta: [{allowed_nodes}, 0]\n"))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("title", "Cafe\u0301"),
        ("title", "Unsafe\x07title"),
        ("aliases", '["Cafe\u0301"]'),
        ("aliases", '["bad\\u0000alias"]'),
    ),
)
def test_non_nfc_or_control_frontmatter_values_are_rejected(field: str, value: str) -> None:
    if field == "title":
        source = _source(title=value)
    else:
        source = _source(extra=f"aliases: {value}\n")
    with pytest.raises(ContentModelError):
        ContentPage.from_markdown("page.md", source)


def test_lone_unicode_surrogate_in_frontmatter_fails_closed() -> None:
    source = '---\ntitle: "\\uD800"\nrole: "axis"\n---\n'
    with pytest.raises(ContentModelError, match="Unicode surrogate"):
        ContentPage.from_markdown("page.md", source)


def test_oversized_yaml_integer_is_wrapped_as_content_model_error() -> None:
    source = _source(extra=f"sequence: {'9' * 5000}\n")
    with pytest.raises(ContentModelError, match="invalid YAML frontmatter"):
        ContentPage.from_markdown("page.md", source)


@pytest.mark.parametrize(
    "path",
    (
        "/absolute.md",
        "../outside.md",
        "folder/../page.md",
        "folder/./page.md",
        "folder//page.md",
        "folder\\page.md",
        "Cafe\u0301.md",
        "page.txt",
    ),
)
def test_page_paths_are_canonical_relative_markdown(path: str) -> None:
    with pytest.raises(ContentModelError, match="content path"):
        ContentPage.from_markdown(path, _source())


@pytest.mark.parametrize(
    "source_pdf",
    ("/srv/source.pdf", "../PDF/source.pdf", "PDF/../source.pdf", "PDF//x.pdf", "PDF\\x.pdf"),
)
def test_source_paths_are_canonical_and_cannot_traverse(source_pdf: str) -> None:
    with pytest.raises(ContentModelError, match="source path"):
        ContentPage.from_markdown("page.md", _source(extra=f"source_pdf: '{source_pdf}'\n"))


def test_commonmark_fence_state_rejects_backtick_info_but_accepts_tilde_info() -> None:
    assert advance_fence_state("```python\n", None) == (("`", 3), True)
    assert advance_fence_state("```bad`info\n", None) == (None, False)
    assert advance_fence_state("~~~bad`info\n", None) == (("~", 3), True)
    assert advance_fence_state("``\n", ("`", 3)) == (("`", 3), True)
    assert advance_fence_state("````\n", ("`", 3)) == (None, True)


def test_page_parse_is_pure_and_source_bytes_remain_stable() -> None:
    source = _source(body="# Heading\n")
    page = ContentPage.from_markdown("page.md", source)
    assert page.source.encode() == source.encode()
