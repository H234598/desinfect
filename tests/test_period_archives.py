from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterator, Mapping

import pytest

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline import rights
from scripts.rki_pipeline.aggregation import (
    AggregationError,
    PeriodManifestError,
    PeriodSelectionError,
    plan_period_archives,
    period_ref,
    render_month_index,
    render_period_manifest,
    select_periods,
    validate_period_manifest,
)
from scripts.rki_pipeline.archive import ArchiveBuild, build_archive
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.manifests import ManifestGraph
from scripts.rki_pipeline.rights import resolve_rights
from scripts.rki_pipeline.storage.base import PreparedObject, RightsStorageAuthorizer
from tests.test_manifests import _build as build_p06_graph
from tests.test_manifests import _document as p06_document
from tests.test_manifests import _second_bitstream as p06_second_bitstream
from tests.test_manifests import _source as p06_source
from tests.test_manifests import _storage as p06_storage


def due(kind: TaskKind, period: str) -> DueTask:
    return DueTask(
        task_id=f"{kind.value}:{period}",
        kind=kind,
        period=period,
        reason="test",
        due_at="2026-01-01T05:00:00Z",
    )


def test_berlin_period_boundaries_have_stable_epochs() -> None:
    week = period_ref(TaskKind.WEEK, "2025-W52")
    month = period_ref(TaskKind.MONTH, "2026-07")
    year = period_ref(TaskKind.YEAR, "2025")
    assert (week.start, week.end, week.source_date_epoch) == (
        date(2025, 12, 22), date(2025, 12, 28), 1766962800
    )
    assert (month.start, month.end, month.source_date_epoch) == (
        date(2026, 7, 1), date(2026, 7, 31), 1785535200
    )
    assert (year.start, year.end, year.source_date_epoch) == (
        date(2025, 1, 1), date(2025, 12, 31), 1767222000
    )


def test_due_and_affected_periods_are_unioned_once_and_sorted() -> None:
    affected = AffectedPeriods(
        weeks={"2025-W52", "2025-W50"},
        months={"2025-12"},
        years={2025},
    )
    periods = select_periods(
        datetime(2026, 1, 5, 5, tzinfo=timezone.utc),
        (due(TaskKind.WEEK, "2025-W52"), due(TaskKind.MONTH, "2025-12")),
        affected,
    )
    assert tuple((item.kind.value, item.value) for item in periods) == (
        ("week", "2025-W50"),
        ("week", "2025-W52"),
        ("month", "2025-12"),
        ("year", "2025"),
    )


@pytest.mark.parametrize("value", [True, 2026, "2026-W00", "2026-W54"])
def test_invalid_affected_week_fails_closed(value: object) -> None:
    affected = AffectedPeriods()
    affected.weeks.add(value)  # type: ignore[arg-type]
    with pytest.raises(PeriodSelectionError):
        select_periods(datetime(2026, 8, 4, tzinfo=timezone.utc), (), affected)


def test_boolean_affected_year_fails_closed() -> None:
    with pytest.raises(PeriodSelectionError):
        select_periods(
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            (),
            AffectedPeriods(years={True}),
        )


def test_naive_as_of_fails_closed() -> None:
    with pytest.raises(PeriodSelectionError):
        select_periods(datetime(2026, 8, 4), (), AffectedPeriods())


def test_period_is_not_closed_immediately_before_berlin_midnight() -> None:
    with pytest.raises(PeriodSelectionError):
        select_periods(
            datetime(2025, 12, 28, 22, 59, 59, tzinfo=timezone.utc),
            (due(TaskKind.WEEK, "2025-W52"),),
            AffectedPeriods(),
        )


@pytest.mark.parametrize(
    ("value", "start", "end", "source_date_epoch"),
    [
        ("2026-03", date(2026, 3, 1), date(2026, 3, 31), 1774994400),
        ("2026-10", date(2026, 10, 1), date(2026, 10, 31), 1793487600),
    ],
)
def test_berlin_dst_month_endpoints_are_stable(
    value: str, start: date, end: date, source_date_epoch: int
) -> None:
    period = period_ref(TaskKind.MONTH, value)
    assert (period.start, period.end, period.source_date_epoch) == (
        start,
        end,
        source_date_epoch,
    )


_SOURCE_ID = "rki:176904/900000001.2"
_DOCUMENT_ID = "rki-176904-900000001-v2"
_DECISION_SHA256 = "d" * 64


def _prepared(
    tmp_path: Path,
    *,
    artifact_id: str,
    logical_key: str,
    payload: bytes,
    source_id: str = _SOURCE_ID,
    source_sha256: str,
    document_id: str = _DOCUMENT_ID,
    conversion_id: str | None = None,
    visibility: str = "repository_authorized",
) -> PreparedObject:
    path = tmp_path / logical_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return PreparedObject(
        artifact_id=artifact_id,
        logical_key=logical_key,
        path=path,
        temp_root=tmp_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        source_id=source_id,
        source_sha256=source_sha256,
        decision_sha256=_DECISION_SHA256,
        visibility=visibility,
        rights_state="approved",
        document_id=document_id,
        conversion_id=conversion_id,
    )


def _plan_inputs(
    tmp_path: Path,
    *,
    markdown: bool = True,
    visibility: str = "repository_authorized",
) -> dict[str, object]:
    pdf_path = "rki/Bulletins/Jahre/2026/PDF/2026-07-10_bulletin.pdf"
    markdown_path = "rki/Bulletins/Jahre/2026/Markdown/2026-07-10_bulletin.md"
    pdf_payload = b"pdf current v2"
    source_sha256 = hashlib.sha256(pdf_payload).hexdigest()
    markdown_payload = b"# current v2\n"
    markdown_sha256 = hashlib.sha256(markdown_payload).hexdigest()
    markdown_id = "conv-" + "a" * 64
    source = {
        "bitstream_id": "rki-bitstream-" + "b" * 64,
        "source_id": _SOURCE_ID,
        "title": "Current | bulletin",
        "handle": "176904/900000001.2",
        "version": 2,
        "publication_date": "2026-07-10",
        "sha256": source_sha256,
        "decision_sha256": _DECISION_SHA256,
        "rights": {"state": "approved"},
    }
    old_source = {
        **source,
        "bitstream_id": "rki-bitstream-" + "c" * 64,
        "source_id": "rki:176904/900000001",
        "handle": "176904/900000001",
        "version": 1,
    }
    document = {
        "document_id": _DOCUMENT_ID,
        "version": 2,
        "source_id": _SOURCE_ID,
        "bitstream_id": source["bitstream_id"],
        "publication_date": "2026-07-10",
        "canonical_periods": {"week": "2026-W28", "month": "2026-07", "year": 2026},
        "paths": {"pdf": pdf_path, "markdown": markdown_path if markdown else None},
        "superseded_by": None,
    }
    old_document = {
        **document,
        "document_id": "rki-176904-900000001-v1",
        "version": 1,
        "source_id": old_source["source_id"],
        "bitstream_id": old_source["bitstream_id"],
        "superseded_by": _DOCUMENT_ID,
    }
    conversion = {
        "conversion_id": markdown_id,
        "document_id": _DOCUMENT_ID,
        "bitstream_id": source["bitstream_id"],
        "source_sha256": source_sha256,
        "state": "converted" if markdown else "not_materialized",
        "output_sha256": markdown_sha256 if markdown else None,
        "storage_reference": markdown_id if markdown else None,
    }
    common = {
        "source_id": _SOURCE_ID,
        "source_sha256": source_sha256,
        "document_id": _DOCUMENT_ID,
        "decision_sha256": _DECISION_SHA256,
        "visibility": visibility,
        "rights_state": "approved",
    }
    storage = [
        {
            **common,
            "artifact_id": "pdf-current-v2",
            "relative_path": pdf_path,
            "sha256": source_sha256,
            "bytes": len(pdf_payload),
            "conversion_id": None,
        }
    ]
    prepared = {
        pdf_path: _prepared(
            tmp_path,
            artifact_id="pdf-current-v2",
            logical_key=pdf_path,
            payload=pdf_payload,
            source_sha256=source_sha256,
            visibility=visibility,
        )
    }
    if markdown:
        storage.append(
            {
                **common,
                "artifact_id": markdown_id,
                "relative_path": markdown_path,
                "sha256": markdown_sha256,
                "bytes": len(markdown_payload),
                "conversion_id": markdown_id,
            }
        )
        prepared[markdown_path] = _prepared(
            tmp_path,
            artifact_id=markdown_id,
            logical_key=markdown_path,
            payload=markdown_payload,
            source_sha256=source_sha256,
            conversion_id=markdown_id,
            visibility=visibility,
        )
    return {
        "as_of": datetime(2026, 8, 4, tzinfo=timezone.utc),
        "due_tasks": (due(TaskKind.MONTH, "2026-07"),),
        "affected_periods": AffectedPeriods(),
        "graph": ManifestGraph(
            sources=(old_source, source),
            documents=(old_document, document),
            conversions=(conversion,),
            storage_references=tuple(storage),
        ),
        "prepared_by_logical_key": prepared,
    }


def test_plan_selects_current_documents_and_separates_formats(tmp_path: Path) -> None:
    plan = plan_period_archives(**_plan_inputs(tmp_path))

    period = plan.periods[0]
    assert tuple(document.document_id for document in period.documents) == (_DOCUMENT_ID,)
    assert tuple(archive.spec.kind for archive in period.archives) == (
        "month-markdown",
        "month-pdf",
    )
    assert all(
        not entry.path.endswith(".zip")
        for archive in period.archives
        for entry in archive.spec.entries
    )


def test_empty_format_is_omitted(tmp_path: Path) -> None:
    pdf_only = plan_period_archives(**_plan_inputs(tmp_path, markdown=False))
    assert tuple(archive.spec.kind for archive in pdf_only.periods[0].archives) == ("month-pdf",)


def test_mixed_visibility_fails_after_exact_storage_prepared_joins(tmp_path: Path) -> None:
    inputs = _mutated_plan_inputs(tmp_path, "basename-collision")
    graph = inputs["graph"]
    prepared = inputs["prepared_by_logical_key"]
    assert isinstance(graph, ManifestGraph)
    assert isinstance(prepared, dict)
    old_path = graph.documents[-1]["paths"]["pdf"]
    assert isinstance(old_path, str)
    new_path = old_path.replace("2026-07-10_bulletin.pdf", "2026-07-10_other.pdf")
    documents = list(graph.documents)
    documents[-1] = {**documents[-1], "paths": {"pdf": new_path, "markdown": None}}
    storage = list(graph.storage_references)
    storage[-1] = {**storage[-1], "relative_path": new_path, "visibility": "public"}
    second_prepared = prepared.pop(old_path)
    prepared[new_path] = replace(
        second_prepared,
        logical_key=new_path,
        visibility="public",
    )
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=tuple(documents),
        conversions=graph.conversions,
        storage_references=tuple(storage),
    )
    with pytest.raises(AggregationError, match="Sichtbarkeit"):
        plan_period_archives(**inputs)


def _mutated_plan_inputs(tmp_path: Path, mutation: str) -> dict[str, object]:
    inputs = _plan_inputs(tmp_path)
    graph = inputs["graph"]
    prepared = inputs["prepared_by_logical_key"]
    assert isinstance(graph, ManifestGraph)
    assert isinstance(prepared, dict)
    storage = list(graph.storage_references)
    conversions = list(graph.conversions)
    if mutation == "missing-storage":
        storage.pop()
    elif mutation == "missing-prepared":
        prepared.pop(next(iter(prepared)))
    elif mutation == "size-drift":
        storage[0] = {**storage[0], "bytes": storage[0]["bytes"] + 1}
    elif mutation == "sha-drift":
        storage[0] = {**storage[0], "sha256": "e" * 64}
    elif mutation == "source-drift":
        storage[0] = {**storage[0], "source_id": "rki:176904/900000002"}
    elif mutation == "document-drift":
        storage[0] = {**storage[0], "document_id": "rki-176904-900000002-v1"}
    elif mutation == "conversion-drift":
        storage[1] = {**storage[1], "conversion_id": "conv-" + "e" * 64}
    elif mutation == "unknown-conversion-state":
        conversions[0] = {**conversions[0], "state": "invented"}
    elif mutation == "basename-collision":
        duplicate = dict(graph.documents[1])
        duplicate.update(
            document_id="rki-176904-900000002-v1",
            version=1,
            source_id="rki:176904/900000002",
            bitstream_id="rki-bitstream-" + "e" * 64,
            paths={
                "pdf": "rki/Bulletins/Jahre/2026/PDF/other/2026-07-10_bulletin.pdf",
                "markdown": None,
            },
        )
        second_payload = b"second pdf"
        second_sha256 = hashlib.sha256(second_payload).hexdigest()
        second_source = {
            **graph.sources[1],
            "bitstream_id": duplicate["bitstream_id"],
            "source_id": duplicate["source_id"],
            "handle": "176904/900000002",
            "version": 1,
            "sha256": second_sha256,
        }
        second_storage = {
            **storage[0],
            "artifact_id": "pdf-current-other",
            "relative_path": duplicate["paths"]["pdf"],
            "sha256": second_sha256,
            "bytes": len(second_payload),
            "source_id": duplicate["source_id"],
            "source_sha256": second_sha256,
            "document_id": duplicate["document_id"],
        }
        second_path = second_storage["relative_path"]
        assert isinstance(second_path, str)
        prepared[second_path] = _prepared(
            tmp_path,
            artifact_id="pdf-current-other",
            logical_key=second_path,
            payload=second_payload,
            source_id=duplicate["source_id"],
            source_sha256=second_sha256,
            document_id=duplicate["document_id"],
        )
        second_conversion = {
            **conversions[0],
            "conversion_id": "conv-" + "f" * 64,
            "document_id": duplicate["document_id"],
            "bitstream_id": duplicate["bitstream_id"],
            "source_sha256": second_sha256,
            "state": "not_materialized",
            "output_sha256": None,
            "storage_reference": None,
        }
        inputs["graph"] = ManifestGraph(
            sources=(*graph.sources, second_source),
            documents=(*graph.documents, duplicate),
            conversions=(*conversions, second_conversion),
            storage_references=(*storage, second_storage),
        )
        return inputs
    else:
        raise AssertionError(f"unbekannte Mutation: {mutation}")
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=graph.documents,
        conversions=tuple(conversions),
        storage_references=tuple(storage),
    )
    return inputs


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-storage", "PreparedObject"),
        ("missing-prepared", "PreparedObject"),
        ("size-drift", "Größe"),
        ("sha-drift", "SHA-256"),
        ("source-drift", "Source"),
        ("document-drift", "Dokument"),
        ("conversion-drift", "Conversion"),
        ("unknown-conversion-state", "Konvertierungsstatus"),
        ("basename-collision", "Kollision"),
    ],
)
def test_manifest_join_drift_fails_closed(tmp_path: Path, mutation: str, message: str) -> None:
    with pytest.raises(AggregationError, match=message):
        plan_period_archives(**_mutated_plan_inputs(tmp_path, mutation))


def test_year_archive_contains_payloads_not_month_zips(tmp_path: Path) -> None:
    inputs = _plan_inputs(tmp_path)
    graph = inputs["graph"]
    assert isinstance(graph, ManifestGraph)
    documents = list(graph.documents)
    documents[1] = {
        **documents[1],
        "canonical_periods": {"week": "2025-W28", "month": "2025-07", "year": 2025},
    }
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=tuple(documents),
        conversions=graph.conversions,
        storage_references=graph.storage_references,
    )
    inputs["due_tasks"] = (due(TaskKind.YEAR, "2025"),)
    year = plan_period_archives(**inputs).periods[0]

    assert year.period.value == "2025"
    assert year.archives
    assert all(
        entry.path.endswith((".pdf", ".md")) and not entry.path.endswith(".zip")
        for archive in year.archives
        for entry in archive.spec.entries
    )


def _prepared_for_graph(
    tmp_path: Path, graph: ManifestGraph
) -> dict[str, PreparedObject]:
    return {
        reference["relative_path"]: PreparedObject(
            artifact_id=reference["artifact_id"],
            logical_key=reference["relative_path"],
            path=tmp_path / reference["relative_path"],
            temp_root=tmp_path,
            sha256=reference["sha256"],
            size=reference["bytes"],
            source_id=reference["source_id"],
            source_sha256=reference["source_sha256"],
            decision_sha256=reference["decision_sha256"],
            visibility=reference["visibility"],
            rights_state=reference["rights_state"],
            document_id=reference["document_id"],
            conversion_id=reference["conversion_id"],
        )
        for reference in graph.storage_references
    }


def test_plan_accepts_valid_p06_alias_and_conversionless_bitstream(tmp_path: Path) -> None:
    alias_source, alias_document, alias_storage = p06_second_bitstream()
    graph = build_p06_graph(
        sources=(p06_source(), alias_source),
        documents=(p06_document(), alias_document),
        storage=(p06_storage()[0], p06_storage()[1], alias_storage),
    )
    plan = plan_period_archives(
        as_of=datetime(2026, 8, 4, tzinfo=timezone.utc),
        due_tasks=(due(TaskKind.MONTH, "1996-03"),),
        affected_periods=AffectedPeriods(),
        graph=graph,
        prepared_by_logical_key=_prepared_for_graph(tmp_path, graph),
    )

    pdf_archive = next(archive for archive in plan.periods[0].archives if archive.spec.kind == "month-pdf")
    assert len(pdf_archive.spec.entries) == 2


def test_plan_resolves_markdown_storage_reference_by_artifact_id(tmp_path: Path) -> None:
    inputs = _plan_inputs(tmp_path)
    graph = inputs["graph"]
    prepared = inputs["prepared_by_logical_key"]
    assert isinstance(graph, ManifestGraph)
    assert isinstance(prepared, dict)
    markdown_path = next(path for path in prepared if path.endswith(".md"))
    artifact_id = "markdown-storage-v2"
    conversions = ({**graph.conversions[0], "storage_reference": artifact_id},)
    storage = list(graph.storage_references)
    storage[1] = {**storage[1], "artifact_id": artifact_id}
    prepared[markdown_path] = replace(prepared[markdown_path], artifact_id=artifact_id)
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=graph.documents,
        conversions=conversions,
        storage_references=tuple(storage),
    )

    plan = plan_period_archives(**inputs)

    assert tuple(archive.spec.kind for archive in plan.periods[0].archives) == (
        "month-markdown",
        "month-pdf",
    )


def test_plan_rejects_graph_foreign_prepared_object(tmp_path: Path) -> None:
    inputs = _plan_inputs(tmp_path)
    prepared = inputs["prepared_by_logical_key"]
    assert isinstance(prepared, dict)
    extra_path = "rki/Bulletins/Jahre/2026/PDF/extra.pdf"
    prepared[extra_path] = _prepared(
        tmp_path,
        artifact_id="extra-pdf",
        logical_key=extra_path,
        payload=b"extra",
        source_sha256=next(iter(prepared.values())).source_sha256,
    )

    with pytest.raises(AggregationError, match="PreparedObject"):
        plan_period_archives(**inputs)


def test_plan_preserves_nonpersisted_failed_conversion_state(tmp_path: Path) -> None:
    inputs = _plan_inputs(tmp_path, markdown=False)
    graph = inputs["graph"]
    assert isinstance(graph, ManifestGraph)
    failed = {
        **graph.conversions[0],
        "state": "failed",
        "output_sha256": None,
        "storage_reference": None,
    }
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=graph.documents,
        conversions=(failed,),
        storage_references=graph.storage_references,
    )

    plan = plan_period_archives(**inputs)

    assert plan.periods[0].documents[0].conversion_state == "failed"


def test_plan_rejects_ambiguous_nonpersisted_conversions(tmp_path: Path) -> None:
    inputs = _plan_inputs(tmp_path, markdown=False)
    graph = inputs["graph"]
    assert isinstance(graph, ManifestGraph)
    conversions = (
        graph.conversions[0],
        {**graph.conversions[0], "conversion_id": "conv-" + "b" * 64},
    )
    inputs["graph"] = ManifestGraph(
        sources=graph.sources,
        documents=graph.documents,
        conversions=conversions,
        storage_references=graph.storage_references,
    )

    with pytest.raises(AggregationError, match="mehrdeutig"):
        plan_period_archives(**inputs)


def _period_builds(
    tmp_path: Path, period, monkeypatch: pytest.MonkeyPatch
) -> dict[str, ArchiveBuild]:
    prepared = tuple(
        entry.prepared for archive in period.archives for entry in archive.spec.entries
    )
    register = tmp_path / "rights-register.yml"
    register.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "decisions:",
                *(
                    line
                    for item in {
                        (entry.source_id, entry.source_sha256) for entry in prepared
                    }
                    for line in (
                        f"  - source_id: {item[0]}",
                        f"    source_sha256: {item[1]}",
                        "    state: approved",
                        "    basis: Reviewed RKI reuse terms",
                        "    reviewed_by: Legal Reviewer",
                        '    reviewed_at: "2026-08-03T08:00:00Z"',
                    )
                ),
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register)
    authorizer = RightsStorageAuthorizer(
        authority=rights.load_rights_authority(),
        policy=rights.load_rights_policy(),
    )
    builds: dict[str, ArchiveBuild] = {}
    for archive in period.archives:
        entries = tuple(
            replace(
                entry,
                prepared=replace(
                    entry.prepared,
                    decision_sha256=resolve_rights(
                        entry.prepared.source_id,
                        entry.prepared.source_sha256,
                        authority=authorizer.authority,
                        policy=authorizer.policy,
                    ).decision_sha256,
                ),
            )
            for entry in archive.spec.entries
        )
        spec = replace(archive.spec, entries=entries)
        path = tmp_path / "built" / archive.spec.archive_id / "archive.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        builds[archive.spec.archive_id] = build_archive(spec, path, authorizer=authorizer)
    return builds


def _month_period(tmp_path: Path, *, markdown: bool = True):
    return plan_period_archives(**_plan_inputs(tmp_path, markdown=markdown)).periods[0]


def test_month_index_is_canonical_complete_and_escaped(tmp_path: Path) -> None:
    base = _month_period(tmp_path)
    document = replace(
        base.documents[0],
        title="A | B <script> & C\r\nD",
        doi="10.1000/example",
    )
    period = replace(base, documents=(document,))

    rendered = render_month_index(period)

    assert rendered.endswith(b"\n")
    assert b"A &#124; B &lt;script&gt; &amp; C  D" in rendered
    assert b"176904/900000001.2" in rendered
    assert b"10.1000/example" in rendered
    assert b"converted" in rendered
    assert b"Artikel: 1" in rendered
    assert document.pdf is not None
    assert document.pdf.sha256.encode("ascii") in rendered
    assert b"RKI-Einzelartikel-2026-07-06_bis_2026-07-12-PDF" in rendered
    assert b"RKI-Einzelartikel-2026-07-27_bis_2026-08-02-PDF" in rendered


def test_month_index_links_cross_boundary_week_and_allows_missing_optional_values(
    tmp_path: Path,
) -> None:
    base = _month_period(tmp_path, markdown=False)
    period = replace(base, documents=(replace(base.documents[0], doi=None),))

    payload = render_month_index(period)

    assert b"2026-07-27_bis_2026-08-02-PDF" in payload
    assert b"| \xe2\x80\x94 |" in payload
    assert b"not_materialized" in payload


def test_period_manifest_is_canonical_backend_neutral_and_order_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _month_period(tmp_path)
    second = replace(
        base.documents[0],
        document_id="rki-176904-900000002-v1",
        source_id="rki:176904/900000002",
        publication_date="2026-07-11",
    )
    period = replace(base, documents=(base.documents[0], second))
    builds = _period_builds(tmp_path, period, monkeypatch)

    payload = render_period_manifest(period, builds)
    value = validate_period_manifest(payload)
    reversed_period = replace(
        period,
        documents=tuple(reversed(period.documents)),
        archives=tuple(reversed(period.archives)),
    )
    reversed_builds = dict(reversed(tuple(builds.items())))

    assert payload == stable_json_dumps(value).encode("utf-8")
    assert payload == render_period_manifest(reversed_period, reversed_builds)
    assert "storage_backend" not in payload.decode("utf-8")
    assert value["archives"] == sorted(value["archives"], key=lambda item: item["archive_id"])


def test_year_manifest_lists_only_selected_document_months(tmp_path: Path) -> None:
    base = _month_period(tmp_path)
    first = replace(base.documents[0], publication_date="2026-01-10")
    second = replace(
        first,
        document_id="rki-176904-900000002-v1",
        source_id="rki:176904/900000002",
        publication_date="2026-02-10",
    )
    period = replace(
        base,
        period=period_ref(TaskKind.YEAR, "2026"),
        documents=(second, first),
        archives=(),
        index_path=None,
        manifest_path="rki/Bulletins/Manifeste/Archive/year/2026.json",
    )

    value = validate_period_manifest(render_period_manifest(period, {}))

    assert value["month_manifests"] == [
        "rki/Bulletins/Manifeste/Archive/month/2026-01.json",
        "rki/Bulletins/Manifeste/Archive/month/2026-02.json",
    ]


def test_period_manifest_rejects_archive_build_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    builds = _period_builds(tmp_path, period, monkeypatch)
    duplicate = {**builds, "duplicate": next(iter(builds.values()))}

    with pytest.raises(PeriodManifestError, match="Archiv-ID"):
        render_period_manifest(period, duplicate)
    for field in ("input_fingerprint", "output_sha256", "size"):
        archive_id, build = next(iter(builds.items()))
        drifted = dict(builds)
        drifted[archive_id] = replace(
            build,
            **{
                field: (
                    "f" * 64 if field != "size" else build.size + 1
                )
            },
        )
        with pytest.raises(PeriodManifestError, match=field):
            render_period_manifest(period, drifted)


def test_manifest_validation_rejects_unknown_and_noncanonical_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    value = json.loads(
        render_period_manifest(period, _period_builds(tmp_path, period, monkeypatch))
    )
    value["unknown"] = True
    with pytest.raises(PeriodManifestError):
        validate_period_manifest(stable_json_dumps(value).encode("utf-8"))

    payload = render_period_manifest(period, _period_builds(tmp_path, period, monkeypatch))
    with pytest.raises(PeriodManifestError, match="kanonisch"):
        validate_period_manifest(payload.replace(b"\n", b"\r\n"))


def test_manifest_fingerprint_binds_document_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    value = json.loads(
        render_period_manifest(period, _period_builds(tmp_path, period, monkeypatch))
    )
    value["documents"][0]["pdf_sha256"] = "e" * 64

    with pytest.raises(PeriodManifestError, match="input_fingerprint"):
        validate_period_manifest(stable_json_dumps(value).encode("utf-8"))


def _manifest_fingerprint_for_test(value: dict[str, object]) -> str:
    normalized = {
        key: (
            [{**archive, "storage_reference": None} for archive in content]
            if key == "archives"
            else content
        )
        for key, content in value.items()
        if key != "input_fingerprint"
    }
    return hashlib.sha256(stable_json_dumps(normalized).encode("utf-8")).hexdigest()


def test_period_manifest_rejects_non_zip_archive_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    builds = _period_builds(tmp_path, period, monkeypatch)
    archive_id, build = next(iter(builds.items()))
    payload = b"not a zip"
    build.path.write_bytes(payload)
    builds[archive_id] = replace(
        build,
        output_sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )

    with pytest.raises(PeriodManifestError, match="Archiv"):
        render_period_manifest(period, builds)


def test_period_manifest_snapshots_adversarial_build_mapping_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    builds = _period_builds(tmp_path, period, monkeypatch)

    class SwitchAfterIteration(Mapping[str, ArchiveBuild]):
        def __init__(self) -> None:
            self.changed = False

        def __iter__(self) -> Iterator[str]:
            self.changed = True
            return iter(builds)

        def __len__(self) -> int:
            return len(builds)

        def __getitem__(self, key: str) -> ArchiveBuild:
            return builds[key]

        def items(self):
            if self.changed:
                return ((key, object()) for key in builds)
            return builds.items()

    assert validate_period_manifest(render_period_manifest(period, SwitchAfterIteration()))


@pytest.mark.parametrize("field", ["document_id", "source_id"])
def test_manifest_validation_rejects_duplicate_document_identity(
    tmp_path: Path, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    value = json.loads(
        render_period_manifest(period, _period_builds(tmp_path, period, monkeypatch))
    )
    second = dict(value["documents"][0])
    second["publication_date"] = "2026-07-11"
    second["pdf_sha256"] = "e" * 64
    if field == "document_id":
        second["source_id"] = "rki:176904/900000002"
    else:
        second["document_id"] = "rki-176904-900000002-v1"
    value["documents"].append(second)
    value["documents"] = sorted(
        value["documents"],
        key=lambda item: (item["publication_date"], item["document_id"], item["source_id"]),
    )
    value["input_fingerprint"] = _manifest_fingerprint_for_test(value)

    with pytest.raises(PeriodManifestError, match=field):
        validate_period_manifest(stable_json_dumps(value).encode("utf-8"))


def test_manifest_validation_rejects_url_storage_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = _month_period(tmp_path)
    value = json.loads(
        render_period_manifest(period, _period_builds(tmp_path, period, monkeypatch))
    )
    value["archives"][0]["storage_reference"] = "https://backend.example/object-key"
    value["input_fingerprint"] = _manifest_fingerprint_for_test(value)

    with pytest.raises(PeriodManifestError):
        validate_period_manifest(stable_json_dumps(value).encode("utf-8"))


def test_month_index_omits_missing_weekly_format_links(tmp_path: Path) -> None:
    period = _month_period(tmp_path, markdown=False)

    payload = render_month_index(period)

    assert b"RKI-Einzelartikel-2026-07-27_bis_2026-08-02-PDF" in payload
    assert b"RKI-Einzelartikel-2026-07-27_bis_2026-08-02-Markdown" not in payload


def test_month_index_percent_encodes_relative_link_targets(tmp_path: Path) -> None:
    base = _month_period(tmp_path)
    assert base.documents[0].pdf is not None
    unsafe_pdf = replace(
        base.documents[0].pdf,
        logical_key="rki/Bulletins/Jahre/2026/PDF/A [x])\n.pdf",
    )
    period = replace(base, documents=(replace(base.documents[0], pdf=unsafe_pdf),))

    payload = render_month_index(period)

    assert b"A%20%5Bx%5D%29%0A.pdf" in payload
    assert b"A [x])\n.pdf" not in payload


def test_month_index_escapes_backslash_and_pipe_without_markdown_ambiguity(
    tmp_path: Path,
) -> None:
    base = _month_period(tmp_path)
    period = replace(base, documents=(replace(base.documents[0], title=r"A\B | C"),))

    payload = render_month_index(period)

    assert b"A\\B &#124; C" in payload
    assert b"A\\B \\| C" not in payload
