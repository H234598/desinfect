#!/usr/bin/env python3
"""Strict period selection for deterministic archive aggregation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind

_BERLIN = ZoneInfo("Europe/Berlin")
_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>0[1-9]|[1-4][0-9]|5[0-3])$")
_MONTH = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_YEAR = re.compile(r"^[0-9]{4}$")
_KIND_ORDER = {TaskKind.WEEK: 0, TaskKind.MONTH: 1, TaskKind.YEAR: 2}


class AggregationError(ValueError):
    """Base aggregation contract failure."""


class PeriodSelectionError(AggregationError):
    """A due or affected period is malformed, future, or not closed."""


@dataclass(frozen=True, slots=True)
class PeriodRef:
    """One immutable, closed calendar period in Berlin time."""

    kind: TaskKind
    value: str
    start: date
    end: date
    source_date_epoch: int


def _period_dates(kind: TaskKind, value: str) -> tuple[date, date]:
    if type(kind) is not TaskKind or kind is TaskKind.RECONCILIATION:
        raise PeriodSelectionError("Period kind ist ungültig")
    if type(value) is not str:
        raise PeriodSelectionError("Period muss eine Zeichenkette sein")
    if kind is TaskKind.WEEK:
        match = _WEEK.fullmatch(value)
        if match is None:
            raise PeriodSelectionError("ISO-Woche ist ungültig")
        try:
            start = date.fromisocalendar(int(match["year"]), int(match["week"]), 1)
        except ValueError as exc:
            raise PeriodSelectionError("ISO-Woche existiert nicht") from exc
        return start, date.fromordinal(start.toordinal() + 6)
    if kind is TaskKind.MONTH:
        match = _MONTH.fullmatch(value)
        if match is None:
            raise PeriodSelectionError("Monat ist ungültig")
        year, month = int(match["year"]), int(match["month"])
        try:
            start = date(year, month, 1)
            next_start = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        except ValueError as exc:
            raise PeriodSelectionError("Monat existiert nicht") from exc
        return start, date.fromordinal(next_start.toordinal() - 1)
    if _YEAR.fullmatch(value) is None:
        raise PeriodSelectionError("Jahr ist ungültig")
    try:
        year = int(value)
        return date(year, 1, 1), date(year, 12, 31)
    except ValueError as exc:
        raise PeriodSelectionError("Jahr existiert nicht") from exc


def period_ref(kind: TaskKind, value: str) -> PeriodRef:
    """Parse one exact week/month/year and derive its Berlin close instant."""

    start, end = _period_dates(kind, value)
    close = datetime.combine(date.fromordinal(end.toordinal() + 1), time.min, _BERLIN)
    return PeriodRef(kind, value, start, end, int(close.timestamp()))


def _affected_values(affected_periods: AffectedPeriods) -> Iterable[tuple[TaskKind, str]]:
    if type(affected_periods) is not AffectedPeriods:
        raise PeriodSelectionError("affected_periods muss ein AffectedPeriods-Wert sein")
    groups = (
        (TaskKind.WEEK, affected_periods.weeks),
        (TaskKind.MONTH, affected_periods.months),
        (TaskKind.YEAR, affected_periods.years),
    )
    for kind, values in groups:
        if type(values) is not set:
            raise PeriodSelectionError("AffectedPeriods-Feld muss eine Menge sein")
        for value in values:
            if kind is TaskKind.YEAR:
                if type(value) is not int:
                    raise PeriodSelectionError("Betroffenes Jahr muss eine Ganzzahl sein")
                yield kind, f"{value:04d}"
            else:
                if type(value) is not str:
                    raise PeriodSelectionError("Betroffene Periode muss eine Zeichenkette sein")
                yield kind, value


def select_periods(
    as_of: datetime,
    due_tasks: Iterable[DueTask],
    affected_periods: AffectedPeriods,
) -> tuple[PeriodRef, ...]:
    """Return closed due/affected periods in stable chronological kind order."""

    if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PeriodSelectionError("as_of muss ein bewusster datetime-Wert sein")
    pairs: set[tuple[TaskKind, str]] = set(_affected_values(affected_periods))
    try:
        iterator = iter(due_tasks)
    except TypeError as exc:
        raise PeriodSelectionError("due_tasks muss iterierbar sein") from exc
    for task in iterator:
        if type(task) is not DueTask:
            raise PeriodSelectionError("due_tasks muss DueTask-Werte enthalten")
        if task.kind is TaskKind.RECONCILIATION:
            raise PeriodSelectionError("Reconciliation ist keine Archivperiode")
        pairs.add((task.kind, task.period))
    selected: list[PeriodRef] = []
    for kind, value in pairs:
        period = period_ref(kind, value)
        if as_of.timestamp() < period.source_date_epoch:
            raise PeriodSelectionError(f"Periode ist noch nicht abgeschlossen: {kind.value}:{value}")
        selected.append(period)
    return tuple(sorted(selected, key=lambda item: (_KIND_ORDER[item.kind], item.start)))
