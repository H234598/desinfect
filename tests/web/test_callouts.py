"""Generated-copy callout conversion contracts."""

from __future__ import annotations

import pytest

from scripts.web.callouts import convert_obsidian_callouts_for_web


@pytest.mark.parametrize(
    ("source_type", "expected_type"),
    (
        ("note", "note"),
        ("summary", "abstract"),
        ("hint", "tip"),
        ("check", "success"),
        ("help", "question"),
        ("caution", "warning"),
        ("fail", "failure"),
        ("error", "danger"),
        ("cite", "quote"),
        ("rights", "rights"),
        ("historical", "historical"),
        ("safety", "safety"),
    ),
)
def test_supported_callouts_convert_deterministically(source_type: str, expected_type: str) -> None:
    source = f"> [!{source_type}] Title\n> Body\n"
    assert convert_obsidian_callouts_for_web(source) == (f'!!! {expected_type} "Title"\n    Body\n')


@pytest.mark.parametrize(("fold", "directive"), (("+", "???+"), ("-", "???"), ("", "!!!")))
def test_folding_and_nested_content_are_preserved(fold: str, directive: str) -> None:
    source = f"> [!warning]{fold} Fold\n> outer\n> > nested\n>\n> final\n"
    assert convert_obsidian_callouts_for_web(source) == (
        f'{directive} warning "Fold"\n    outer\n    > nested\n    \n    final\n'
    )


def test_unknown_and_malformed_callouts_remain_literal() -> None:
    source = "> [!unknown] Literal\n> body\n> [!warning Missing bracket\n"
    assert convert_obsidian_callouts_for_web(source) == source


def test_callouts_inside_fences_inline_code_and_comments_remain_byte_identical() -> None:
    source = (
        "```md\n> [!warning] fenced\n> body\n```\n"
        "`start\n> [!warning] inline\n> body\nend`\n"
        "<!--\n> [!warning] commented\n> body\n-->\n"
    )
    assert convert_obsidian_callouts_for_web(source) == source


def test_multiline_html_comment_inside_callout_leaves_entire_block_literal() -> None:
    source = (
        "> [!note] Outer\n"
        "> before\n"
        "> <!-- hidden starts\n"
        "> [!warning] hidden\n"
        "> hidden body\n"
        "> -->\n"
        "> after\n"
    )
    assert convert_obsidian_callouts_for_web(source) == source


def test_indented_callout_syntax_is_code_and_remains_literal() -> None:
    source = "    > [!warning] hidden\n    > body\n"
    assert convert_obsidian_callouts_for_web(source) == source


def test_inline_code_later_in_callout_title_does_not_hide_callout() -> None:
    source = "> [!note] Run `command`\n> body\n"
    converted = convert_obsidian_callouts_for_web(source)
    assert converted.startswith('!!! note "Run \\`command\\`"\n')


def test_unknown_callout_block_remains_literal() -> None:
    source = "> [!custom] Literal\n> body\n> [!warning] nested literal\n"
    assert convert_obsidian_callouts_for_web(source) == source


def test_unknown_indented_block_does_not_consume_following_top_level_callout() -> None:
    source = "  > [!custom] Literal\n  > body\n> [!warning] Convert\n> body\n"
    converted = convert_obsidian_callouts_for_web(source)
    assert converted.startswith("  > [!custom] Literal\n  > body\n")
    assert converted.endswith('!!! warning "Convert"\n    body\n')


def test_titles_escape_html_markdown_quotes_and_backslashes() -> None:
    source = '> [!safety] <img> & [label] "quote" \\ path\n> body\n'
    assert convert_obsidian_callouts_for_web(source) == (
        '!!! safety "&lt;img&gt; &amp; \\[label\\] &quot;quote&quot; \\\\ path"\n    body\n'
    )


def test_title_quotes_render_as_html_entities_not_visible_backslashes() -> None:
    source = '> [!note] Hinweis zu "Kontakt"\n> body\n'
    assert convert_obsidian_callouts_for_web(source) == (
        '!!! note "Hinweis zu &quot;Kontakt&quot;"\n    body\n'
    )


def test_lone_unicode_surrogate_in_callout_title_stays_literal() -> None:
    source = "> [!note] \ud800\n> body\n"
    assert convert_obsidian_callouts_for_web(source) == source


def test_conversion_is_pure_repeatable_and_leaves_source_unchanged() -> None:
    source = "> [!rights] Rechte\n> Text\n"
    first = convert_obsidian_callouts_for_web(source)
    second = convert_obsidian_callouts_for_web(source)
    assert first == second
    assert source == "> [!rights] Rechte\n> Text\n"
