from datetime import date, datetime, timezone

import pytest

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline.aggregation import PeriodSelectionError, period_ref, select_periods
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind


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
