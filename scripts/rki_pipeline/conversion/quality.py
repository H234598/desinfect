"""Pure, deterministic text-extraction quality assessment."""

from __future__ import annotations

from dataclasses import dataclass

MIN_CHARACTERS_PER_PAGE = 40
MAX_REPLACEMENT_RATIO = 0.01


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    quality: str
    character_count: int
    replacement_ratio: float
    empty_pages: tuple[int, ...]
    reasons: tuple[str, ...]


def assess_quality(
    pages: tuple[str, ...],
    *,
    expected_page_count: int,
) -> QualityAssessment:
    """Assess page coverage, visible text volume, and replacement damage."""

    if type(expected_page_count) is not int or expected_page_count <= 0:
        raise ValueError("expected_page_count muss positiv sein")
    if type(pages) is not tuple or not all(type(page) is str for page in pages):
        raise TypeError("pages muss ein String-Tupel sein")

    visible = tuple("".join(character for character in page if not character.isspace()) for page in pages)
    character_count = sum(len(page) for page in visible)
    replacement_count = sum(page.count("\ufffd") for page in visible)
    replacement_ratio = replacement_count / character_count if character_count else 0.0
    empty_pages = tuple(index for index, page in enumerate(visible, start=1) if not page)
    reasons: list[str] = []
    if len(pages) != expected_page_count:
        reasons.append("page_count_mismatch")
    if any(len(page) < MIN_CHARACTERS_PER_PAGE for page in visible):
        reasons.append("too_few_characters")
    if replacement_ratio > MAX_REPLACEMENT_RATIO:
        reasons.append("replacement_ratio")
    if empty_pages:
        reasons.append("empty_pages")
    return QualityAssessment(
        quality="needs_review" if reasons else "good",
        character_count=character_count,
        replacement_ratio=replacement_ratio,
        empty_pages=empty_pages,
        reasons=tuple(reasons),
    )
