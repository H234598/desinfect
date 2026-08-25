"""Fail-closed P10.3 table data and server-rendering contracts."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.web.build_tables import (
    CorpusRow,
    TableBuildError,
    TableInputs,
    load_table_inputs,
    render_table_fragment,
    render_table_page,
    write_table_data_assets,
)
from scripts.web import build_tables as build_tables_module


class _RenderedTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.header_counts: list[int] = []
        self.row_cell_counts: list[list[int]] = []
        self._table = -1
        self._in_body = False
        self._row_cells: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "table":
            self._table += 1
            self.header_counts.append(0)
            self.row_cell_counts.append([])
        elif tag == "th":
            self.header_counts[self._table] += 1
        elif tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            self._row_cells = 0
        elif tag == "td" and self._row_cells is not None:
            self._row_cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row_cells is not None:
            self.row_cell_counts[self._table].append(self._row_cells)
            self._row_cells = None
        elif tag == "tbody":
            self._in_body = False


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json_dumps(payload), encoding="utf-8")


def _status(*, gate: bool, state: str) -> dict[str, object]:
    source = Path(__file__).parents[2] / "status.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    corpus = payload["corpus"]
    corpus["taxonomy_gate_satisfied"] = gate
    corpus["taxonomy_state"] = state
    if gate:
        corpus["inventory_complete_through_year"] = 2020
        corpus["analysis_corpus_complete_through_year"] = 2020
        payload["periods"]["last_completed_month"] = "2020-12"
        payload["periods"]["last_completed_year"] = 2020
        payload["periods"]["last_reconciliation_at"] = "2026-08-01T00:00:00Z"
    return payload


def _readiness(*, gate: bool = True) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "policy_version": "1.0.0",
        "minimum_required_year": 2020,
        "inventory_complete_through_year": 2020 if gate else None,
        "analysis_corpus_complete_through_year": 2020 if gate else None,
        "public_mirror_complete_through_year": None,
        "monthly_archives_complete_through": "2020-12" if gate else None,
        "yearly_archives_complete_through_year": 2020 if gate else None,
        "unresolved_source_gaps": 0,
        "approved_source_exceptions": 2,
        "unresolved_conversion_failures": 0,
        "last_reconciliation_conclusion": "success" if gate else "not_run",
        "taxonomy_gate_satisfied": gate,
        "based_on_manifest_sha256": "a" * 64 if gate else None,
    }


def _taxonomy(*, status: str = "approved", review_status: str = "approved") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "version": "1.2.3",
        "status": status,
        "based_on_corpus_through_year": 2020,
        "based_on_manifest_sha256": "a" * 64,
        "categories": [
            {
                "id": "hand-hygiene",
                "label": "Handhygiene",
                "dimension": "area",
                "definition": "Nur synthetische Testkategorie.",
                "aliases": [],
                "evidence_count": 1,
                "source_examples": ["synthetic-test"],
                "review_status": review_status,
            }
        ],
        "migrations": [],
    }


def _corpus_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_type": "bulletin",
        "title": "Beispiel",
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
    row.update(changes)
    return row


def _instruction_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "effectiveness_rank": 2,
        "application_area": "Hände",
        "title": "Anleitung B",
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
    row.update(changes)
    return row


def _write_status(repo: Path, *, gate: bool, state: str) -> None:
    _write_json(repo / "status.json", _status(gate=gate, state=state))


def _write_corpus(repo: Path, rows: list[dict[str, object]]) -> None:
    _write_json(
        repo / "content/generated-data/corpus-table.json",
        {"schema_version": "1.0.0", "rows": rows},
    )


def _write_taxonomy(repo: Path, payload: dict[str, object]) -> None:
    path = repo / "research/taxonomy.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8")


def _write_approved_state(repo: Path, *, instructions: list[dict[str, object]]) -> None:
    _write_status(repo, gate=True, state="approved")
    _write_json(repo / "research/corpus-readiness.json", _readiness())
    _write_taxonomy(repo, _taxonomy())
    _write_json(
        repo / "content/generated-data/anleitungen.json",
        {
            "schema_version": "1.0.0",
            "taxonomy_version": "1.2.3",
            "rows": instructions,
        },
    )


def test_closed_gate_renders_corpus_rows_only(tmp_path: Path) -> None:
    """Unescaped cells or a premature instruction block make this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    _write_corpus(tmp_path, [_corpus_row(title="A < B")])

    inputs = load_table_inputs(tmp_path)
    rendered = render_table_fragment(inputs)

    assert "A &lt; B" in rendered
    assert "Nur Metadaten" in rendered
    assert "https://edoc.rki.de/handle/176904/1234" in rendered
    assert "<a " not in rendered
    assert "Anleitungstabelle" not in rendered


def test_missing_corpus_projection_renders_visible_empty_state(tmp_path: Path) -> None:
    """Invented fallback rows or an invisible empty state make this fail."""

    _write_status(tmp_path, gate=False, state="blocked")

    rendered = render_table_fragment(load_table_inputs(tmp_path))

    assert "Noch keine validierten Dokumentmanifeste" in rendered
    assert "<tbody>\n</tbody>" in rendered


def test_open_gate_requires_matching_approved_evidence(tmp_path: Path) -> None:
    """Treating the status flag alone as publication approval makes this fail."""

    _write_status(tmp_path, gate=True, state="approved")

    with pytest.raises(TableBuildError, match="corpus-readiness"):
        load_table_inputs(tmp_path)


def test_status_registry_and_readiness_gate_disagreement_fail_closed(tmp_path: Path) -> None:
    """Skipping either registry validation or gate equality makes this fail."""

    invalid_status = _status(gate=False, state="blocked")
    invalid_status["extra"] = True
    _write_json(tmp_path / "status.json", invalid_status)
    with pytest.raises(TableBuildError, match="status"):
        load_table_inputs(tmp_path)

    _write_status(tmp_path, gate=False, state="blocked")
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness(gate=True))
    with pytest.raises(TableBuildError, match="disagree"):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize("state", ("blocked", "candidate"))
def test_open_gate_review_states_render_only_corpus(tmp_path: Path, state: str) -> None:
    """Readiness review must stay green without publishing instructions."""

    _write_status(tmp_path, gate=True, state=state)
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness())

    inputs = load_table_inputs(tmp_path)

    assert inputs.readiness_satisfied is True
    assert inputs.publication_ready is False
    assert inputs.instruction_rows == ()


def test_proposal_state_requires_matching_proposal_taxonomy(tmp_path: Path) -> None:
    """Missing or approved taxonomy during proposal review makes this fail."""

    _write_status(tmp_path, gate=True, state="proposal")
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness())
    with pytest.raises(TableBuildError, match="taxonomy"):
        load_table_inputs(tmp_path)

    _write_taxonomy(tmp_path, _taxonomy())
    with pytest.raises(TableBuildError, match="proposal taxonomy"):
        load_table_inputs(tmp_path)

    _write_taxonomy(tmp_path, _taxonomy(status="proposal", review_status="candidate"))
    assert load_table_inputs(tmp_path).instruction_rows == ()

    drifted = _taxonomy(status="proposal", review_status="candidate")
    drifted["based_on_manifest_sha256"] = "b" * 64
    _write_taxonomy(tmp_path, drifted)
    with pytest.raises(TableBuildError, match="manifest"):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize("premature", ("taxonomy", "instructions"))
def test_closed_gate_rejects_premature_active_inputs(tmp_path: Path, premature: str) -> None:
    """Ignoring an out-of-state optional file makes this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    if premature == "taxonomy":
        _write_taxonomy(tmp_path, _taxonomy(status="proposal", review_status="candidate"))
    else:
        _write_json(
            tmp_path / "content/generated-data/anleitungen.json",
            {"schema_version": "1.0.0", "taxonomy_version": "1.2.3", "rows": []},
        )

    with pytest.raises(TableBuildError):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"unexpected": "x"}, "keys"),
        ({"year": True}, "year"),
        ({"month": 13}, "month"),
        ({"title": "bad\ncell"}, "control"),
        ({"checksum": "A" * 64}, "checksum"),
        ({"rights_state": "public"}, "rights"),
        ({"source": "https://example.org/handle/176904/1"}, "source"),
    ),
)
def test_corpus_rows_fail_closed_on_noncanonical_values(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    """Weak row validation would accept the named malformed field."""

    _write_status(tmp_path, gate=False, state="blocked")
    _write_corpus(tmp_path, [_corpus_row(**change)])

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


def test_corpus_projection_rejects_duplicate_identity_and_unknown_root_key(tmp_path: Path) -> None:
    """Last-write-wins rows or permissive projection roots make this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    row = _corpus_row()
    _write_corpus(tmp_path, [row, dict(row)])
    with pytest.raises(TableBuildError, match="duplicate"):
        load_table_inputs(tmp_path)

    _write_json(
        tmp_path / "content/generated-data/corpus-table.json",
        {"schema_version": "1.0.0", "rows": [], "extra": True},
    )
    with pytest.raises(TableBuildError, match="keys"):
        load_table_inputs(tmp_path)


def test_projection_schema_versions_must_match_contract(tmp_path: Path) -> None:
    """Unknown corpus or instruction schemas must not be interpreted."""

    _write_status(tmp_path, gate=False, state="blocked")
    _write_json(
        tmp_path / "content/generated-data/corpus-table.json",
        {"schema_version": "9.9.9", "rows": []},
    )
    with pytest.raises(TableBuildError, match="corpus schema version"):
        load_table_inputs(tmp_path)

    _write_corpus(tmp_path, [])
    _write_approved_state(tmp_path, instructions=[])
    _write_json(
        tmp_path / "content/generated-data/anleitungen.json",
        {"schema_version": "9.9.9", "taxonomy_version": "1.2.3", "rows": []},
    )
    with pytest.raises(TableBuildError, match="instruction schema version"):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    ("rows", "message"),
    (
        ([_instruction_row(unexpected="x")], "keys"),
        ([_instruction_row(title="bad\ncell")], "control"),
        ([_instruction_row(effectiveness_rank=True)], "integer"),
    ),
)
def test_instruction_rows_fail_closed_on_noncanonical_values(
    tmp_path: Path,
    rows: list[dict[str, object]],
    message: str,
) -> None:
    """Permissive instruction-field validation makes this fail."""

    _write_approved_state(tmp_path, instructions=rows)

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


def test_instruction_projection_rejects_duplicate_identity_and_unknown_root_key(
    tmp_path: Path,
) -> None:
    """Duplicate instruction identities or extra root fields make this fail."""

    row = _instruction_row()
    _write_approved_state(tmp_path, instructions=[row, dict(row)])
    with pytest.raises(TableBuildError, match="duplicate"):
        load_table_inputs(tmp_path)

    _write_json(
        tmp_path / "content/generated-data/anleitungen.json",
        {
            "schema_version": "1.0.0",
            "taxonomy_version": "1.2.3",
            "rows": [],
            "extra": True,
        },
    )
    with pytest.raises(TableBuildError, match="keys"):
        load_table_inputs(tmp_path)


def test_semantic_approved_state_renders_sorted_complete_server_rows_and_assets(
    tmp_path: Path,
) -> None:
    """Global ranking, missing server rows, or noncanonical assets make this fail."""

    _write_corpus(
        tmp_path,
        [
            _corpus_row(title="Alt", year=2019, month=None, rki_handle=None),
            _corpus_row(title="Neu", year=2021, month=2, rki_handle="176904/2"),
        ],
    )
    _write_approved_state(
        tmp_path,
        instructions=[
            _instruction_row(title="Niedrig", effectiveness_rank=1, confidence="low"),
            _instruction_row(
                title="Fläche hoch",
                application_area="Flächen",
                effectiveness_rank=99,
            ),
            _instruction_row(title="Hoch", effectiveness_rank=3, confidence="medium"),
        ],
    )

    inputs = load_table_inputs(tmp_path)
    rendered = render_table_fragment(inputs)
    parser = _RenderedTableParser()
    parser.feed(rendered)
    parser.close()
    stage = tmp_path / "stage"
    stage.mkdir()
    write_table_data_assets(stage, inputs)

    assert [row.title for row in inputs.corpus_rows] == ["Neu", "Alt"]
    assert [row.title for row in inputs.instruction_rows] == ["Fläche hoch", "Hoch", "Niedrig"]
    assert rendered.index("Fläche hoch") < rendered.index("Hoch") < rendered.index("Niedrig")
    assert "Rangfolgen gelten nur innerhalb" in rendered
    assert parser.header_counts == [14, 13]
    assert parser.row_cell_counts == [[14, 14], [13, 13, 13]]
    corpus_asset = json.loads((stage / "assets/data/corpus-table.json").read_text("utf-8"))
    instruction_asset = json.loads((stage / "assets/data/anleitungen.json").read_text("utf-8"))
    assert [row["title"] for row in corpus_asset["rows"]] == ["Neu", "Alt"]
    assert instruction_asset["taxonomy_version"] == "1.2.3"


def test_projection_sorting_is_independent_of_input_order(tmp_path: Path) -> None:
    """Stable-sort fallback to source order for equal normalized keys makes this fail."""

    results: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    corpus = [
        _corpus_row(title="alpha", rki_handle="176904/1"),
        _corpus_row(title="Alpha", rki_handle="176904/1"),
    ]
    instructions = [
        _instruction_row(title="Gleich", bulletin="Bulletin B", page="2"),
        _instruction_row(title="Gleich", bulletin="Bulletin A", page="1"),
    ]
    for name, corpus_rows, instruction_rows in (
        ("forward", corpus, instructions),
        ("reverse", list(reversed(corpus)), list(reversed(instructions))),
    ):
        repo = tmp_path / name
        _write_corpus(repo, corpus_rows)
        _write_approved_state(repo, instructions=instruction_rows)
        inputs = load_table_inputs(repo)
        results.append(
            (
                tuple(row.title for row in inputs.corpus_rows),
                tuple(row.bulletin for row in inputs.instruction_rows),
            )
        )

    assert results == [
        (("Alpha", "alpha"), ("Bulletin A", "Bulletin B")),
        (("Alpha", "alpha"), ("Bulletin A", "Bulletin B")),
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda readiness, taxonomy, projection: readiness.update(unresolved_source_gaps=1),
            "gaps",
        ),
        (
            lambda readiness, taxonomy, projection: readiness.update(
                last_reconciliation_conclusion="failed"
            ),
            "reconciliation",
        ),
        (
            lambda readiness, taxonomy, projection: taxonomy.update(
                based_on_manifest_sha256="b" * 64
            ),
            "manifest",
        ),
        (
            lambda readiness, taxonomy, projection: projection.update(taxonomy_version="9.9.9"),
            "version",
        ),
        (
            lambda readiness, taxonomy, projection: readiness.update(
                inventory_complete_through_year=2019
            ),
            "inventory",
        ),
        (
            lambda readiness, taxonomy, projection: readiness.update(
                monthly_archives_complete_through="2020-11"
            ),
            "monthly",
        ),
        (
            lambda readiness, taxonomy, projection: readiness.update(
                unresolved_conversion_failures=1
            ),
            "conversion",
        ),
    ),
)
def test_approved_state_requires_complete_consistent_evidence(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    """Publishing across an evidence gap or version drift makes this fail."""

    readiness = _readiness()
    taxonomy = _taxonomy()
    projection: dict[str, object] = {
        "schema_version": "1.0.0",
        "taxonomy_version": "1.2.3",
        "rows": [_instruction_row()],
    }
    mutate(readiness, taxonomy, projection)
    _write_status(tmp_path, gate=True, state="approved")
    _write_json(tmp_path / "research/corpus-readiness.json", readiness)
    _write_taxonomy(tmp_path, taxonomy)
    _write_json(tmp_path / "content/generated-data/anleitungen.json", projection)

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    (
        ("corpus", "inventory_complete_through_year", 2019, "status inventory"),
        ("corpus", "analysis_corpus_complete_through_year", 2019, "status analysis"),
        ("periods", "last_completed_month", "2020-11", "status monthly"),
        ("periods", "last_completed_year", 2019, "status yearly"),
    ),
)
def test_approved_state_requires_status_completion_thresholds(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    message: str,
) -> None:
    """Approved publication must independently verify all status thresholds."""

    _write_approved_state(tmp_path, instructions=[])
    status = _status(gate=True, state="approved")
    status[section][field] = value
    _write_json(tmp_path / "status.json", status)

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("analysis_corpus_complete_through_year", "readiness analysis"),
        ("yearly_archives_complete_through_year", "readiness yearly"),
    ),
)
def test_approved_state_requires_readiness_completion_thresholds(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    """Approved publication must independently verify readiness thresholds."""

    _write_approved_state(tmp_path, instructions=[])
    readiness = _readiness()
    readiness[field] = 2019
    _write_json(tmp_path / "research/corpus-readiness.json", readiness)

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


def test_approved_state_requires_complete_approved_taxonomy_and_projection(tmp_path: Path) -> None:
    """Stale evidence, pending category review, or missing projection blocks publication."""

    _write_approved_state(tmp_path, instructions=[])
    taxonomy = _taxonomy()
    taxonomy["based_on_corpus_through_year"] = 2019
    _write_taxonomy(tmp_path, taxonomy)
    with pytest.raises(TableBuildError, match="based_on_corpus_through_year"):
        load_table_inputs(tmp_path)

    _write_taxonomy(tmp_path, _taxonomy(review_status="candidate"))
    with pytest.raises(TableBuildError, match="non-approved category"):
        load_table_inputs(tmp_path)

    (tmp_path / "content/generated-data/anleitungen.json").unlink()
    _write_taxonomy(tmp_path, _taxonomy())
    with pytest.raises(TableBuildError, match="instruction projection"):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    "categories",
    (["unknown-category"], ["hand-hygiene", "hand-hygiene"], ["z", "a"]),
)
def test_instruction_categories_are_approved_unique_and_sorted(
    tmp_path: Path,
    categories: list[str],
) -> None:
    """Unknown, duplicate, or unordered category IDs make this fail."""

    _write_approved_state(
        tmp_path,
        instructions=[_instruction_row(derived_categories=categories)],
    )

    with pytest.raises(TableBuildError, match="categor"):
        load_table_inputs(tmp_path)


def test_instruction_effectiveness_rank_accepts_any_positive_integer(tmp_path: Path) -> None:
    """An invented machine-integer ceiling on the contextual rank makes this fail."""

    _write_approved_state(
        tmp_path,
        instructions=[_instruction_row(effectiveness_rank=2**63)],
    )

    assert load_table_inputs(tmp_path).instruction_rows[0].effectiveness_rank == 2**63


def test_approved_taxonomy_rejects_duplicate_category_ids(tmp_path: Path) -> None:
    """Two approved definitions sharing one category identity make this fail."""

    _write_approved_state(tmp_path, instructions=[_instruction_row()])
    taxonomy = _taxonomy()
    duplicate = json.loads(json.dumps(taxonomy["categories"][0]))
    duplicate["label"] = "Andere Definition"
    taxonomy["categories"].append(duplicate)
    _write_taxonomy(tmp_path, taxonomy)

    with pytest.raises(TableBuildError, match="category"):
        load_table_inputs(tmp_path)


def test_table_marker_must_exist_exactly_once() -> None:
    """A missing or ambiguous replacement point makes this fail."""

    inputs = TableInputs((), (), None, False, False)
    with pytest.raises(TableBuildError, match="marker"):
        render_table_page("no marker", inputs)
    with pytest.raises(TableBuildError, match="marker"):
        render_table_page("<!-- DESINFECT_TABLE -->\n<!-- DESINFECT_TABLE -->", inputs)
    assert render_table_page("before\n<!-- DESINFECT_TABLE -->\nafter", inputs).count("<table") == 1


def test_all_corpus_text_cells_are_escaped_without_links() -> None:
    """Any raw external cell or data-driven href makes this fail."""

    row = CorpusRow(
        document_type='<img src=x onerror="x">',
        title="A & B",
        year=2020,
        month=None,
        rki_handle="<handle>",
        doi='"doi"',
        rights_state="unknown",
        pdf_present=False,
        markdown_status="<md>",
        ocr_status="<ocr>",
        monthly_archive_present=False,
        yearly_archive_present=False,
        checksum=None,
        source="https://edoc.rki.de/handle/176904/&quot;",
    )
    rendered = render_table_fragment(TableInputs((row,), (), None, False, False))

    assert "<img" not in rendered
    assert "&lt;img src=x onerror=&quot;x&quot;&gt;" in rendered
    assert "<a " not in rendered


def test_json_depth_and_yaml_alias_budgets_fail_closed(tmp_path: Path) -> None:
    """Unbounded JSON depth or YAML expansion makes this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    nested: object = "leaf"
    for _ in range(18):
        nested = {"nested": nested}
    _write_json(
        tmp_path / "content/generated-data/corpus-table.json",
        {"schema_version": "1.0.0", "rows": [], "nested": nested},
    )
    with pytest.raises(TableBuildError, match="depth"):
        load_table_inputs(tmp_path)

    _write_status(tmp_path, gate=True, state="proposal")
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness())
    taxonomy_path = tmp_path / "research/taxonomy.yml"
    taxonomy_path.write_text("shared: &shared [x]\ncopied: *shared\n", encoding="utf-8")
    with pytest.raises(TableBuildError, match="YAML"):
        load_table_inputs(tmp_path)


def test_json_structure_node_budget_is_enforced(tmp_path: Path) -> None:
    """More than 250,000 JSON structure nodes must fail before row validation."""

    _write_status(tmp_path, gate=False, state="blocked")
    path = tmp_path / "content/generated-data/corpus-table.json"
    path.parent.mkdir(parents=True)
    groups = ["0,0,0,0,0"] * 50_001
    path.write_text(
        '{"schema_version":"1.0.0","rows":[' + ",\n".join(groups) + "]}",
        encoding="utf-8",
    )

    with pytest.raises(TableBuildError, match="node count"):
        load_table_inputs(tmp_path)


def test_structure_budget_rejects_oversized_list_before_expanding_work_stack() -> None:
    """A certainly oversized list must fail before duplicating all child references."""

    class OversizedList(list[object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("oversized list was expanded")

    values = OversizedList([None] * (build_tables_module.MAX_STRUCTURE_NODES + 1))

    with pytest.raises(TableBuildError, match="node count"):
        build_tables_module._check_structure_budget(values, name="synthetic")


@pytest.mark.parametrize(
    ("document", "message"),
    (
        ("root: " + ("[" * 18) + "leaf" + ("]" * 18) + "\n", "depth"),
        ("items:\n" + ("  - [x, x, x, x, x]\n" * 50_001), "node"),
    ),
)
def test_yaml_structure_budgets_are_enforced(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    """Taxonomy YAML must enforce 16-level and 250,000-node budgets."""

    _write_status(tmp_path, gate=True, state="proposal")
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness())
    path = tmp_path / "research/taxonomy.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")

    with pytest.raises(TableBuildError, match=message):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    "document",
    (
        "key: &anchor value\n",
        "base: &base {key: value}\nmerged: {<<: *base}\n",
        "? [complex, key]\n: value\n",
        "duplicate: one\nduplicate: two\n",
        "---\nkey: one\n---\nkey: two\n",
    ),
)
def test_taxonomy_yaml_rejects_ambiguous_structure(tmp_path: Path, document: str) -> None:
    """Anchors, merges, complex/duplicate keys, or multiple documents make this fail."""

    _write_status(tmp_path, gate=True, state="proposal")
    _write_json(tmp_path / "research/corpus-readiness.json", _readiness())
    path = tmp_path / "research/taxonomy.yml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(TableBuildError, match="taxonomy YAML"):
        load_table_inputs(tmp_path)


def test_corpus_cell_length_budget_is_enforced(tmp_path: Path) -> None:
    """An unbounded rendered cell makes this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    _write_corpus(tmp_path, [_corpus_row(title="x" * 4097)])

    with pytest.raises(TableBuildError, match="length"):
        load_table_inputs(tmp_path)


def test_projection_row_and_physical_line_budgets_are_enforced(tmp_path: Path) -> None:
    """Unbounded row count or one giant physical line makes this fail."""

    _write_status(tmp_path, gate=False, state="blocked")
    _write_corpus(tmp_path, [{} for _ in range(20_001)])
    with pytest.raises(TableBuildError, match="bounded list"):
        load_table_inputs(tmp_path)

    path = tmp_path / "content/generated-data/corpus-table.json"
    path.write_text('{"padding":"' + ("x" * 65_537) + '"}', encoding="utf-8")
    with pytest.raises(TableBuildError, match="physical line"):
        load_table_inputs(tmp_path)

    path.write_text(" \n" * 100_001, encoding="utf-8")
    with pytest.raises(TableBuildError, match="physical line count"):
        load_table_inputs(tmp_path)


@pytest.mark.parametrize(
    ("relative", "size", "gate", "state"),
    (
        ("status.json", 256 * 1024, False, "blocked"),
        ("research/corpus-readiness.json", 256 * 1024, False, "blocked"),
        ("research/taxonomy.yml", 2 * 1024 * 1024, True, "proposal"),
        ("content/generated-data/corpus-table.json", 8 * 1024 * 1024, False, "blocked"),
        ("content/generated-data/anleitungen.json", 8 * 1024 * 1024, True, "approved"),
    ),
)
def test_table_input_byte_caps_are_enforced(
    tmp_path: Path,
    relative: str,
    size: int,
    gate: bool,
    state: str,
) -> None:
    """Each input must reject exactly one byte beyond its documented cap."""

    _write_status(tmp_path, gate=gate, state=state)
    if gate:
        _write_json(tmp_path / "research/corpus-readiness.json", _readiness())
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (size + 1))

    with pytest.raises(TableBuildError, match="Größenlimit"):
        load_table_inputs(tmp_path)


def test_instruction_row_and_category_count_budgets_are_enforced(tmp_path: Path) -> None:
    """Instruction projections cap rows at 20,000 and categories at 128."""

    _write_approved_state(tmp_path, instructions=[{} for _ in range(20_001)])
    with pytest.raises(TableBuildError, match="bounded list"):
        load_table_inputs(tmp_path)

    _write_approved_state(
        tmp_path,
        instructions=[
            _instruction_row(derived_categories=[f"category-{index}" for index in range(129)])
        ],
    )
    with pytest.raises(TableBuildError, match="categories must be a bounded list"):
        load_table_inputs(tmp_path)
