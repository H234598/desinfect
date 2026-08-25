"""Fail-closed table projections and complete server-rendered P10.3 HTML."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import html
from pathlib import Path, PurePosixPath
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.resolver import BaseResolver

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    atomic_write_text,
    parse_strict_json_object,
    read_bounded_utf8_text_beneath,
    stable_json_dumps,
)
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


STATUS_MAX_BYTES = 256 * 1024
READINESS_MAX_BYTES = 256 * 1024
TAXONOMY_MAX_BYTES = 2 * 1024 * 1024
TABLE_MAX_BYTES = 8 * 1024 * 1024
MAX_PHYSICAL_LINES = 100_000
MAX_PHYSICAL_LINE_BYTES = 64 * 1024
MAX_STRUCTURE_DEPTH = 16
MAX_STRUCTURE_NODES = 250_000
MAX_TABLE_ROWS = 20_000
MAX_CELL_CHARACTERS = 4_096
MAX_DERIVED_CATEGORIES = 128
TABLE_MARKER = "<!-- DESINFECT_TABLE -->"

_RIGHTS_LABELS = {
    "approved": "Freigegeben",
    "metadata_only": "Nur Metadaten",
    "internal_only": "Nur intern",
    "unknown": "Ungeklärt",
    "takedown": "Entfernt",
}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
_RKI_PATH_PREFIXES = ("/handle/176904/", "/bitstream/handle/176904/")
_CORPUS_HEADERS = (
    ("Dokumenttyp", "document_type", "text"),
    ("Titel", "title", "text"),
    ("Jahr", "year", "number"),
    ("Monat", "month", "number"),
    ("RKI-Handle", "rki_handle", "text"),
    ("DOI", "doi", "text"),
    ("Rechtezustand", "rights_state", "text"),
    ("PDF vorhanden", "pdf_present", "text"),
    ("Markdownstatus", "markdown_status", "text"),
    ("OCR-Status", "ocr_status", "text"),
    ("Monatsarchiv", "monthly_archive_present", "text"),
    ("Jahresarchiv", "yearly_archive_present", "text"),
    ("Checksumme", "checksum", "text"),
    ("Quelle", "source", "text"),
)
_INSTRUCTION_HEADERS = (
    ("Rang", "effectiveness_rank", "number"),
    ("Anwendungsbereich", "application_area", "text"),
    ("Titel", "title", "text"),
    ("Wirkstoff", "active_ingredient", "text"),
    ("Konzentration", "concentration", "text"),
    ("Einwirkzeit", "contact_time", "text"),
    ("Spektrum", "spectrum", "text"),
    ("Kategorien", "derived_categories", "text"),
    ("Jahr", "year", "number"),
    ("Zeitstatus", "temporal_status", "text"),
    ("Vertrauen", "confidence", "confidence"),
    ("Bulletin", "bulletin", "text"),
    ("Seite", "page", "text"),
)


class TableBuildError(ValueError):
    """Table inputs, state transitions or rendered output violate P10.3."""


@dataclass(frozen=True, slots=True)
class CorpusRow:
    document_type: str
    title: str
    year: int
    month: int | None
    rki_handle: str | None
    doi: str | None
    rights_state: str
    pdf_present: bool
    markdown_status: str
    ocr_status: str
    monthly_archive_present: bool
    yearly_archive_present: bool
    checksum: str | None
    source: str


@dataclass(frozen=True, slots=True)
class InstructionRow:
    effectiveness_rank: int
    application_area: str
    title: str
    active_ingredient: str
    concentration: str
    contact_time: str
    spectrum: str
    derived_categories: tuple[str, ...]
    year: int
    temporal_status: str
    confidence: str
    bulletin: str
    page: str


@dataclass(frozen=True, slots=True)
class TableInputs:
    corpus_rows: tuple[CorpusRow, ...]
    instruction_rows: tuple[InstructionRow, ...]
    taxonomy_version: str | None
    readiness_satisfied: bool
    publication_ready: bool


_CORPUS_KEYS = frozenset(field.name for field in fields(CorpusRow))
_INSTRUCTION_KEYS = frozenset(field.name for field in fields(InstructionRow))


class _BoundedUniqueLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        self._compose_depth = 0
        self._composed_nodes = 0
        super().__init__(stream)

    def compose_node(
        self,
        parent: yaml.nodes.Node | None,
        index: object,
    ) -> yaml.nodes.Node:
        event = self.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ComposerError(
                None, None, "YAML anchors and aliases are not allowed", event.start_mark
            )
        self._compose_depth += 1
        self._composed_nodes += 1
        try:
            if self._compose_depth > MAX_STRUCTURE_DEPTH:
                raise ComposerError(None, None, "YAML depth budget exceeded", event.start_mark)
            if self._composed_nodes > MAX_STRUCTURE_NODES:
                raise ComposerError(None, None, "YAML node budget exceeded", event.start_mark)
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1


def _construct_unique_mapping(
    loader: _BoundedUniqueLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ConstructorError(
                "while constructing taxonomy",
                node.start_mark,
                "YAML merge keys are not allowed",
                key_node.start_mark,
            )
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing taxonomy",
                node.start_mark,
                "taxonomy keys must be strings",
                key_node.start_mark,
            )
        if key in result:
            raise ConstructorError(
                "while constructing taxonomy",
                node.start_mark,
                f"duplicate taxonomy key: {key}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_BoundedUniqueLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _check_physical_budget(text: str, *, name: str) -> None:
    lines = text.splitlines()
    if len(lines) > MAX_PHYSICAL_LINES:
        raise TableBuildError(f"{name}: physical line count exceeds budget")
    if any(len(line.encode("utf-8")) > MAX_PHYSICAL_LINE_BYTES for line in lines):
        raise TableBuildError(f"{name}: physical line length exceeds budget")


def _check_structure_budget(value: object, *, name: str) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_STRUCTURE_DEPTH:
            raise TableBuildError(f"{name}: structure depth exceeds budget")
        if nodes > MAX_STRUCTURE_NODES:
            raise TableBuildError(f"{name}: structure node count exceeds budget")
        if isinstance(current, dict):
            for key, item in current.items():
                nodes += 1
                if nodes > MAX_STRUCTURE_NODES:
                    raise TableBuildError(f"{name}: structure node count exceeds budget")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            if nodes + len(current) > MAX_STRUCTURE_NODES:
                raise TableBuildError(f"{name}: structure node count exceeds budget")
            stack.extend((item, depth + 1) for item in current)


def _read_text(
    repo_root: Path,
    relative: PurePosixPath,
    *,
    max_bytes: int,
    required: bool,
) -> str | None:
    try:
        return read_bounded_utf8_text_beneath(repo_root, relative, max_bytes=max_bytes)
    except FileNotFoundError:
        if required:
            raise TableBuildError(f"required input is missing: {relative}") from None
        return None
    except (OSError, UnsafePathError, ValueError) as exc:
        raise TableBuildError(f"invalid input {relative}: {exc}") from exc


def _load_json(
    repo_root: Path,
    relative: PurePosixPath,
    *,
    max_bytes: int,
    required: bool,
) -> dict[str, Any] | None:
    text = _read_text(repo_root, relative, max_bytes=max_bytes, required=required)
    if text is None:
        return None
    name = relative.as_posix()
    _check_physical_budget(text, name=name)
    try:
        payload = parse_strict_json_object(text)
    except (RecursionError, ValueError) as exc:
        raise TableBuildError(f"{name}: invalid strict JSON: {exc}") from exc
    _check_structure_budget(payload, name=name)
    return payload


def _load_taxonomy(repo_root: Path) -> dict[str, Any] | None:
    relative = PurePosixPath("research/taxonomy.yml")
    text = _read_text(repo_root, relative, max_bytes=TAXONOMY_MAX_BYTES, required=False)
    if text is None:
        return None
    _check_physical_budget(text, name=relative.as_posix())
    loader = _BoundedUniqueLoader(text)
    try:
        payload = loader.get_single_data()
    except (RecursionError, yaml.YAMLError) as exc:
        raise TableBuildError(f"taxonomy YAML is invalid: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(payload, dict):
        raise TableBuildError("taxonomy YAML root must be a mapping")
    return payload


def _validate_registered(name: str, payload: dict[str, Any]) -> None:
    try:
        validate_document(name, payload)
    except SchemaContractError as exc:
        raise TableBuildError(str(exc)) from exc


def _require_keys(payload: dict[str, Any], expected: frozenset[str], *, name: str) -> None:
    if set(payload) != expected:
        raise TableBuildError(f"{name} keys must be exact")


def _text(value: object, *, name: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise TableBuildError(f"{name} must be non-empty text")
    if len(value) > MAX_CELL_CHARACTERS:
        raise TableBuildError(f"{name} length exceeds budget")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise TableBuildError(f"{name} contains a control character")
    return value


def _integer(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        limit = f" through {maximum}" if maximum is not None else ""
        raise TableBuildError(f"{name} must be an integer from {minimum}{limit}")
    return value


def _boolean(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise TableBuildError(f"{name} must be boolean")
    return value


def _rki_source(value: object) -> str:
    source = _text(value, name="corpus source")
    assert source is not None
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except ValueError as exc:
        raise TableBuildError("corpus source is not a canonical RKI URL") from exc
    if (
        not source.startswith("https://")
        or parsed.scheme != "https"
        or parsed.netloc != "edoc.rki.de"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not any(parsed.path.startswith(prefix) for prefix in _RKI_PATH_PREFIXES)
        or parsed.path in _RKI_PATH_PREFIXES
    ):
        raise TableBuildError("corpus source is not a canonical RKI URL")
    return source


def _parse_corpus_rows(payload: dict[str, Any] | None) -> tuple[CorpusRow, ...]:
    if payload is None:
        return ()
    _require_keys(payload, frozenset({"schema_version", "rows"}), name="corpus root")
    if payload["schema_version"] != "1.0.0":
        raise TableBuildError("corpus schema version must be 1.0.0")
    values = payload["rows"]
    if not isinstance(values, list) or len(values) > MAX_TABLE_ROWS:
        raise TableBuildError("corpus rows must be a bounded list")
    rows: list[CorpusRow] = []
    identities: set[tuple[object, ...]] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise TableBuildError(f"corpus row {index} must be an object")
        _require_keys(value, _CORPUS_KEYS, name=f"corpus row {index}")
        month_value = value["month"]
        month = (
            None
            if month_value is None
            else _integer(month_value, name=f"corpus row {index} month", minimum=1, maximum=12)
        )
        rights_state = _text(value["rights_state"], name=f"corpus row {index} rights")
        if rights_state not in _RIGHTS_LABELS:
            raise TableBuildError(f"corpus row {index} rights state is invalid")
        checksum = _text(value["checksum"], name=f"corpus row {index} checksum", nullable=True)
        if checksum is not None and _CHECKSUM.fullmatch(checksum) is None:
            raise TableBuildError(f"corpus row {index} checksum is invalid")
        row = CorpusRow(
            document_type=_text(value["document_type"], name=f"corpus row {index} document_type"),
            title=_text(value["title"], name=f"corpus row {index} title"),
            year=_integer(
                value["year"], name=f"corpus row {index} year", minimum=1990, maximum=9999
            ),
            month=month,
            rki_handle=_text(
                value["rki_handle"], name=f"corpus row {index} rki_handle", nullable=True
            ),
            doi=_text(value["doi"], name=f"corpus row {index} doi", nullable=True),
            rights_state=rights_state,
            pdf_present=_boolean(value["pdf_present"], name=f"corpus row {index} pdf_present"),
            markdown_status=_text(
                value["markdown_status"], name=f"corpus row {index} markdown_status"
            ),
            ocr_status=_text(value["ocr_status"], name=f"corpus row {index} ocr_status"),
            monthly_archive_present=_boolean(
                value["monthly_archive_present"],
                name=f"corpus row {index} monthly_archive_present",
            ),
            yearly_archive_present=_boolean(
                value["yearly_archive_present"],
                name=f"corpus row {index} yearly_archive_present",
            ),
            checksum=checksum,
            source=_rki_source(value["source"]),
        )
        identity = (row.document_type, row.year, row.month, row.rki_handle, row.title)
        if identity in identities:
            raise TableBuildError("duplicate corpus row identity")
        identities.add(identity)
        rows.append(row)
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                -row.year,
                -(row.month or 0),
                row.document_type.casefold(),
                row.document_type,
                row.title.casefold(),
                row.title,
                row.rki_handle or "",
            ),
        )
    )


def _parse_instruction_rows(
    payload: dict[str, Any],
    *,
    allowed_categories: frozenset[str],
) -> tuple[str, tuple[InstructionRow, ...]]:
    _require_keys(
        payload,
        frozenset({"schema_version", "taxonomy_version", "rows"}),
        name="instruction root",
    )
    if payload["schema_version"] != "1.0.0":
        raise TableBuildError("instruction schema version must be 1.0.0")
    taxonomy_version = _text(payload["taxonomy_version"], name="instruction taxonomy version")
    assert taxonomy_version is not None
    values = payload["rows"]
    if not isinstance(values, list) or len(values) > MAX_TABLE_ROWS:
        raise TableBuildError("instruction rows must be a bounded list")
    rows: list[InstructionRow] = []
    identities: set[tuple[str, str, str, str]] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise TableBuildError(f"instruction row {index} must be an object")
        _require_keys(value, _INSTRUCTION_KEYS, name=f"instruction row {index}")
        categories_value = value["derived_categories"]
        if not isinstance(categories_value, list) or len(categories_value) > MAX_DERIVED_CATEGORIES:
            raise TableBuildError(f"instruction row {index} categories must be a bounded list")
        categories = tuple(
            _text(item, name=f"instruction row {index} category {category_index}")
            for category_index, item in enumerate(categories_value)
        )
        if tuple(sorted(categories)) != categories or len(set(categories)) != len(categories):
            raise TableBuildError(f"instruction row {index} categories must be unique and sorted")
        if not set(categories).issubset(allowed_categories):
            raise TableBuildError(f"instruction row {index} category is not approved")
        confidence = _text(value["confidence"], name=f"instruction row {index} confidence")
        if confidence not in _CONFIDENCE_ORDER:
            raise TableBuildError(f"instruction row {index} confidence is invalid")
        temporal_status = _text(
            value["temporal_status"], name=f"instruction row {index} temporal_status"
        )
        if temporal_status not in {"current", "historical"}:
            raise TableBuildError(f"instruction row {index} temporal status is invalid")
        row = InstructionRow(
            effectiveness_rank=_integer(
                value["effectiveness_rank"],
                name=f"instruction row {index} effectiveness_rank",
                minimum=1,
            ),
            application_area=_text(
                value["application_area"], name=f"instruction row {index} application_area"
            ),
            title=_text(value["title"], name=f"instruction row {index} title"),
            active_ingredient=_text(
                value["active_ingredient"], name=f"instruction row {index} active_ingredient"
            ),
            concentration=_text(
                value["concentration"], name=f"instruction row {index} concentration"
            ),
            contact_time=_text(value["contact_time"], name=f"instruction row {index} contact_time"),
            spectrum=_text(value["spectrum"], name=f"instruction row {index} spectrum"),
            derived_categories=categories,
            year=_integer(
                value["year"], name=f"instruction row {index} year", minimum=1990, maximum=9999
            ),
            temporal_status=temporal_status,
            confidence=confidence,
            bulletin=_text(value["bulletin"], name=f"instruction row {index} bulletin"),
            page=_text(value["page"], name=f"instruction row {index} page"),
        )
        identity = (row.application_area, row.title, row.bulletin, row.page)
        if identity in identities:
            raise TableBuildError("duplicate instruction row identity")
        identities.add(identity)
        rows.append(row)
    return taxonomy_version, tuple(
        sorted(
            rows,
            key=lambda row: (
                row.application_area.casefold(),
                row.application_area,
                -row.effectiveness_rank,
                _CONFIDENCE_ORDER[row.confidence],
                -row.year,
                row.title.casefold(),
                row.title,
                row.bulletin,
                row.page,
            ),
        )
    )


def _require_publication_evidence(
    status: dict[str, Any],
    readiness: dict[str, Any],
    taxonomy: dict[str, Any],
) -> None:
    corpus = status["corpus"]
    periods = status["periods"]
    thresholds = (
        (corpus["inventory_complete_through_year"], 2020, "status inventory"),
        (corpus["analysis_corpus_complete_through_year"], 2020, "status analysis"),
        (periods["last_completed_month"], "2020-12", "status monthly archives"),
        (periods["last_completed_year"], 2020, "status yearly archives"),
        (readiness["inventory_complete_through_year"], 2020, "readiness inventory"),
        (readiness["analysis_corpus_complete_through_year"], 2020, "readiness analysis"),
        (readiness["monthly_archives_complete_through"], "2020-12", "readiness monthly archives"),
        (readiness["yearly_archives_complete_through_year"], 2020, "readiness yearly archives"),
    )
    for actual, required, name in thresholds:
        if actual is None or actual < required:
            raise TableBuildError(f"{name} does not reach 2020")
    if readiness["unresolved_source_gaps"] != 0:
        raise TableBuildError("readiness has unresolved source gaps")
    if readiness["unresolved_conversion_failures"] != 0:
        raise TableBuildError("readiness has unresolved conversion failures")
    if readiness["last_reconciliation_conclusion"] != "success":
        raise TableBuildError("readiness reconciliation is not successful")
    manifest = readiness["based_on_manifest_sha256"]
    if manifest is None or taxonomy["based_on_manifest_sha256"] != manifest:
        raise TableBuildError("readiness and taxonomy manifest hashes drift")
    if taxonomy["based_on_corpus_through_year"] < 2020:
        raise TableBuildError("taxonomy corpus evidence does not reach 2020")


def load_table_inputs(repo_root: Path) -> TableInputs:
    """Load bounded projections and enforce the explicit state matrix."""

    status = _load_json(
        repo_root,
        PurePosixPath("status.json"),
        max_bytes=STATUS_MAX_BYTES,
        required=True,
    )
    assert status is not None
    _validate_registered("status", status)
    readiness = _load_json(
        repo_root,
        PurePosixPath("research/corpus-readiness.json"),
        max_bytes=READINESS_MAX_BYTES,
        required=False,
    )
    if readiness is not None:
        _validate_registered("corpus-readiness", readiness)
    taxonomy = _load_taxonomy(repo_root)
    if taxonomy is not None:
        _validate_registered("taxonomy", taxonomy)
    corpus_payload = _load_json(
        repo_root,
        PurePosixPath("content/generated-data/corpus-table.json"),
        max_bytes=TABLE_MAX_BYTES,
        required=False,
    )
    instruction_payload = _load_json(
        repo_root,
        PurePosixPath("content/generated-data/anleitungen.json"),
        max_bytes=TABLE_MAX_BYTES,
        required=False,
    )
    corpus_rows = _parse_corpus_rows(corpus_payload)

    status_gate = status["corpus"]["taxonomy_gate_satisfied"]
    state = status["corpus"]["taxonomy_state"]
    if readiness is not None and readiness["taxonomy_gate_satisfied"] != status_gate:
        raise TableBuildError("status and corpus-readiness gate values disagree")
    if not status_gate:
        if state not in {"blocked", "candidate"}:
            raise TableBuildError("taxonomy state requires satisfied readiness")
        if taxonomy is not None or instruction_payload is not None:
            raise TableBuildError("taxonomy or instruction input exists before readiness")
        return TableInputs(corpus_rows, (), None, False, False)
    if readiness is None:
        raise TableBuildError("corpus-readiness is required when status gate is true")
    if state in {"blocked", "candidate"}:
        if taxonomy is not None or instruction_payload is not None:
            raise TableBuildError("active taxonomy or instructions conflict with review state")
        return TableInputs(corpus_rows, (), None, True, False)
    if state == "proposal":
        if taxonomy is None or taxonomy["status"] != "proposal":
            raise TableBuildError("proposal taxonomy is required for proposal state")
        if taxonomy["based_on_manifest_sha256"] != readiness["based_on_manifest_sha256"]:
            raise TableBuildError("proposal taxonomy and readiness manifest hashes drift")
        if instruction_payload is not None:
            raise TableBuildError("instructions are forbidden during taxonomy proposal review")
        return TableInputs(corpus_rows, (), taxonomy["version"], True, False)
    if state != "approved" or taxonomy is None or taxonomy["status"] != "approved":
        raise TableBuildError("approved state requires approved taxonomy")
    if any(category["review_status"] != "approved" for category in taxonomy["categories"]):
        raise TableBuildError("approved taxonomy contains a non-approved category")
    category_ids = [category["id"] for category in taxonomy["categories"]]
    if len(category_ids) != len(set(category_ids)):
        raise TableBuildError("approved taxonomy contains a duplicate category id")
    if instruction_payload is None:
        raise TableBuildError("approved state requires instruction projection")
    _require_publication_evidence(status, readiness, taxonomy)
    allowed_categories = frozenset(category_ids)
    taxonomy_version, instruction_rows = _parse_instruction_rows(
        instruction_payload,
        allowed_categories=allowed_categories,
    )
    if taxonomy_version != taxonomy["version"]:
        raise TableBuildError("instruction taxonomy version drifts from approved taxonomy version")
    return TableInputs(corpus_rows, instruction_rows, taxonomy_version, True, True)


def _escaped(value: object | None) -> str:
    if value is None:
        return "—"
    if type(value) is bool:
        return "Ja" if value else "Nein"
    return html.escape(str(value), quote=True)


def _render_table(
    *,
    kind: str,
    label: str,
    caption: str,
    headers: tuple[tuple[str, str, str], ...],
    rows: tuple[tuple[object | None, ...], ...],
) -> str:
    lines = [
        f'<div class="table-region" role="region" tabindex="0" aria-label="{html.escape(label, quote=True)}">',
        f'<table data-enhance-table="{kind}">',
        f"<caption>{html.escape(caption, quote=True)}</caption>",
        "<thead>",
        "<tr>",
        *(
            f'<th scope="col" data-column="{column}" data-sort-type="{sort_type}">'
            f"{html.escape(header, quote=True)}</th>"
            for header, column, sort_type in headers
        ),
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for row in rows:
        lines.extend(("<tr>", *(f"<td>{_escaped(value)}</td>" for value in row), "</tr>"))
    lines.extend(("</tbody>", "</table>", "</div>"))
    return "\n".join(lines)


def _row_values(
    row: CorpusRow | InstructionRow,
    headers: tuple[tuple[str, str, str], ...],
) -> tuple[object | None, ...]:
    values: list[object | None] = []
    for _label, column, _sort_type in headers:
        value = getattr(row, column)
        if column == "rights_state":
            value = _RIGHTS_LABELS[value]
        elif column == "derived_categories":
            value = ", ".join(value)
        values.append(value)
    return tuple(values)


def render_table_fragment(inputs: TableInputs) -> str:
    """Return complete escaped server HTML in deterministic row order."""

    parts = [
        "<h2>Korpustabelle</h2>",
        (
            f"<p>{len(inputs.corpus_rows)} validierte Dokumentmanifeste sind serverseitig dargestellt.</p>"
            if inputs.corpus_rows
            else "<p>Noch keine validierten Dokumentmanifeste</p>"
        ),
        _render_table(
            kind="corpus",
            label="Korpustabelle mit Dokumentmanifesten",
            caption="Validierter Dokumentkorpus",
            headers=_CORPUS_HEADERS,
            rows=tuple(_row_values(row, _CORPUS_HEADERS) for row in inputs.corpus_rows),
        ),
    ]
    if inputs.publication_ready:
        parts.extend(
            (
                "<h2>Anleitungstabelle</h2>",
                "<p>Rangfolgen gelten nur innerhalb des dokumentierten Anwendungskontexts; "
                "es gibt keinen universellen Wirksamkeitspunktwert.</p>",
                _render_table(
                    kind="instructions",
                    label="Anleitungstabelle nach Anwendungskontext",
                    caption="Fachlich freigegebene Anleitungen",
                    headers=_INSTRUCTION_HEADERS,
                    rows=tuple(
                        _row_values(row, _INSTRUCTION_HEADERS) for row in inputs.instruction_rows
                    ),
                ),
            )
        )
    return "\n\n".join(parts) + "\n"


def render_table_page(source: str, inputs: TableInputs) -> str:
    """Replace exactly one canonical marker with the server fragment."""

    if source.count(TABLE_MARKER) != 1:
        raise TableBuildError("table marker must occur exactly once")
    return source.replace(TABLE_MARKER, render_table_fragment(inputs))


def write_table_data_assets(stage: Path, inputs: TableInputs) -> None:
    """Write canonical public JSON projections beneath the held Docs stage."""

    corpus = {
        "schema_version": "1.0.0",
        "rows": [asdict(row) for row in inputs.corpus_rows],
    }
    try:
        atomic_write_text(
            stage / "assets/data/corpus-table.json",
            stable_json_dumps(corpus),
            allowed_root=stage,
        )
        if inputs.publication_ready:
            instructions = {
                "schema_version": "1.0.0",
                "taxonomy_version": inputs.taxonomy_version,
                "rows": [asdict(row) for row in inputs.instruction_rows],
            }
            atomic_write_text(
                stage / "assets/data/anleitungen.json",
                stable_json_dumps(instructions),
                allowed_root=stage,
            )
    except (OSError, UnsafePathError, ValueError) as exc:
        raise TableBuildError(f"cannot write table data assets: {exc}") from exc
