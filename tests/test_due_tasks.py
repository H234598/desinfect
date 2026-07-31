"""Contracts for pure dispatcher watermarks and catch-up calculation."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rki_pipeline.due_tasks import (
    BackfillLimits,
    DispatchLimits,
    DueTaskError,
    TaskKind,
    calculate_backfill_tasks,
    calculate_due_tasks,
    load_dispatcher_config,
)

ROOT = Path(__file__).resolve().parents[1]


def limits() -> DispatchLimits:
    return DispatchLimits(8, 3, 1, 1, 92)


def status(
    *,
    last_week: str | None = None,
    last_month: str | None = None,
    last_year: int | None = None,
    reconciliation: str | None = None,
) -> dict[str, object]:
    return {
        "periods": {
            "last_completed_week": last_week,
            "last_completed_month": last_month,
            "last_completed_year": last_year,
            "last_reconciliation_at": reconciliation,
        }
    }


def test_week_catchup_stops_at_last_closed_iso_week() -> None:
    tasks = calculate_due_tasks(
        status(last_week="2026-W29", last_month="2026-06", last_year=2025, reconciliation="2026-07-01T00:00:00Z"),
        "2026-07-31T12:00:00Z",
        limits(),
    )
    assert [task.period for task in tasks if task.kind is TaskKind.WEEK] == ["2026-W30"]


def test_missing_watermarks_do_not_start_1994_backfill() -> None:
    tasks = calculate_due_tasks(status(), "2026-07-31T12:00:00Z", limits())
    assert [task.period for task in tasks if task.kind is TaskKind.WEEK] == ["2026-W30"]
    assert [task.period for task in tasks if task.kind is TaskKind.MONTH] == ["2026-06"]
    assert [task.period for task in tasks if task.kind is TaskKind.YEAR] == ["2025"]
    assert sum(task.kind is TaskKind.RECONCILIATION for task in tasks) == 1


def test_iso_year_rollover_is_ordered_oldest_first() -> None:
    tasks = calculate_due_tasks(
        status(last_week="2025-W52", last_month="2025-11", last_year=2024, reconciliation="2025-12-31T00:00:00Z"),
        "2026-01-12T01:00:00Z",
        limits(),
    )
    weeks = [task.period for task in tasks if task.kind is TaskKind.WEEK]
    assert weeks == ["2026-W01", "2026-W02"]
    assert tuple(task.kind for task in tasks) == tuple(sorted((task.kind for task in tasks), key=lambda kind: {TaskKind.WEEK: 0, TaskKind.MONTH: 1, TaskKind.YEAR: 2, TaskKind.RECONCILIATION: 3}[kind]))


def test_catchup_is_bounded() -> None:
    bounded = DispatchLimits(2, 1, 1, 1, 92)
    tasks = calculate_due_tasks(
        status(last_week="2025-W01", last_month="2025-01", last_year=2020, reconciliation="2026-07-30T00:00:00Z"),
        "2026-07-31T12:00:00Z",
        bounded,
    )
    assert len([task for task in tasks if task.kind is TaskKind.WEEK]) == 2
    assert len([task for task in tasks if task.kind is TaskKind.MONTH]) == 1
    assert len([task for task in tasks if task.kind is TaskKind.YEAR]) == 1


def test_reconciliation_uses_complete_utc_days() -> None:
    before = calculate_due_tasks(
        status(last_week="2026-W30", last_month="2026-06", last_year=2025, reconciliation="2026-05-01T12:00:01Z"),
        "2026-08-01T12:00:00Z",
        limits(),
    )
    after = calculate_due_tasks(
        status(last_week="2026-W30", last_month="2026-06", last_year=2025, reconciliation="2026-05-01T12:00:00Z"),
        "2026-08-01T12:00:00Z",
        limits(),
    )
    assert not any(task.kind is TaskKind.RECONCILIATION for task in before)
    assert any(task.kind is TaskKind.RECONCILIATION for task in after)


@pytest.mark.parametrize(
    "payload",
    (
        status(last_week="2026-W53"),
        status(last_month="2026-13"),
        status(last_year=True),
        status(reconciliation="2026-01-01T00:00:00+00:00"),
    ),
)
def test_malformed_watermarks_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(DueTaskError):
        calculate_due_tasks(payload, "2026-07-31T12:00:00Z", limits())


def test_future_watermark_fails_closed() -> None:
    with pytest.raises(DueTaskError, match="Zukunft"):
        calculate_due_tasks(status(last_week="2026-W31"), "2026-07-31T12:00:00Z", limits())


def test_explicit_backfill_is_bounded_and_deterministic() -> None:
    values = calculate_backfill_tasks(
        from_year=1994,
        to_year=1996,
        due_at="2026-07-31T12:00:00Z",
        limits=BackfillLimits(max_tasks=3, minimum_year=1994),
    )
    assert [task.task_id for task in values] == ["year:1994", "year:1995", "year:1996"]
    with pytest.raises(DueTaskError, match="überschreitet"):
        calculate_backfill_tasks(
            from_year=1994,
            to_year=1997,
            due_at="2026-07-31T12:00:00Z",
            limits=BackfillLimits(max_tasks=3, minimum_year=1994),
        )


def test_dispatcher_config_is_strict() -> None:
    config = load_dispatcher_config(ROOT / "config" / "dispatcher.toml")
    assert config.daily.max_weeks == 8
    assert config.backfill.minimum_year == 1994


def test_boolean_limits_are_rejected() -> None:
    with pytest.raises(DueTaskError):
        DispatchLimits(True, 1, 1, 1, 92)
