#!/usr/bin/env python3
"""Pure UTC period, watermark, catch-up, and backfill task calculation."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable

_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>0[1-9]|[1-4][0-9]|5[0-3])$")
_MONTH = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_UTC = re.compile(r"Z$")


class DueTaskError(ValueError):
    """A status watermark, dispatcher configuration, or period is invalid."""


class TaskKind(StrEnum):
    """Task classes understood by the scheduling infrastructure."""

    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    RECONCILIATION = "reconciliation"


_KIND_ORDER = {
    TaskKind.WEEK: 0,
    TaskKind.MONTH: 1,
    TaskKind.YEAR: 2,
    TaskKind.RECONCILIATION: 3,
}


@dataclass(frozen=True, slots=True)
class DispatchLimits:
    """Strict per-run limits that prevent unbounded catch-up."""

    max_weeks: int
    max_months: int
    max_years: int
    max_reconciliations: int
    reconciliation_interval_days: int

    def __post_init__(self) -> None:
        for name in (
            "max_weeks",
            "max_months",
            "max_years",
            "max_reconciliations",
            "reconciliation_interval_days",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise DueTaskError(f"{name} muss eine positive Ganzzahl sein")
        if self.max_weeks > 520 or self.max_months > 120 or self.max_years > 100:
            raise DueTaskError("Daily Catch-up-Grenze ist unplausibel groß")
        if not 7 <= self.reconciliation_interval_days <= 366:
            raise DueTaskError("reconciliation_interval_days liegt außerhalb 7..366")


@dataclass(frozen=True, slots=True)
class BackfillLimits:
    """Strict bounds for manually requested historical task sets."""

    max_tasks: int
    minimum_year: int

    def __post_init__(self) -> None:
        if type(self.max_tasks) is not int or not 1 <= self.max_tasks <= 10_000:
            raise DueTaskError("backfill.max_tasks liegt außerhalb 1..10000")
        if type(self.minimum_year) is not int or not 1900 <= self.minimum_year <= 9999:
            raise DueTaskError("backfill.minimum_year ist ungültig")


@dataclass(frozen=True, slots=True)
class DispatcherConfig:
    """Complete immutable dispatcher configuration."""

    daily: DispatchLimits
    backfill: BackfillLimits


@dataclass(frozen=True, slots=True)
class DueTask:
    """One deterministic due unit with no executable code or external data."""

    task_id: str
    kind: TaskKind
    period: str
    reason: str
    due_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TaskKind):
            raise DueTaskError("kind muss ein TaskKind sein")
        if type(self.task_id) is not str or self.task_id != f"{self.kind.value}:{self.period}":
            raise DueTaskError("task_id stimmt nicht mit kind/period überein")
        _validate_period(self.kind, self.period)
        if type(self.reason) is not str or not self.reason or len(self.reason) > 120:
            raise DueTaskError("reason muss eine kurze nichtleere Zeichenkette sein")
        parse_utc(self.due_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "period": self.period,
            "reason": self.reason,
            "due_at": self.due_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DueTask:
        expected = {"task_id", "kind", "period", "reason", "due_at"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise DueTaskError("DueTask besitzt nicht exakt die erwarteten Felder")
        if not all(type(payload[name]) is str for name in expected):
            raise DueTaskError("DueTask-Felder müssen Zeichenketten sein")
        try:
            kind = TaskKind(payload["kind"])
        except ValueError as exc:
            raise DueTaskError(f"Unbekannter TaskKind: {payload['kind']}") from exc
        return cls(
            task_id=payload["task_id"],
            kind=kind,
            period=payload["period"],
            reason=payload["reason"],
            due_at=payload["due_at"],
        )


def _exact_table(data: dict[str, Any], name: str, expected: set[str]) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise DueTaskError(f"[{name}] muss eine TOML-Tabelle sein")
    if set(value) != expected:
        raise DueTaskError(
            f"[{name}] Schlüssel driften: erwartet {sorted(expected)}, gefunden {sorted(value)}"
        )
    return value


def load_dispatcher_config(path: Path) -> DispatcherConfig:
    """Load an exact-key TOML configuration without permissive coercion."""

    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DueTaskError(f"Dispatcher-Konfiguration ist nicht lesbar: {path}") from exc
    if set(data) != {"schema_version", "daily", "backfill"}:
        raise DueTaskError("Dispatcher-Konfiguration besitzt unbekannte oder fehlende Schlüssel")
    if data["schema_version"] != 1:
        raise DueTaskError("Unbekannte Dispatcher-Konfigurationsversion")
    daily = _exact_table(
        data,
        "daily",
        {
            "max_weeks",
            "max_months",
            "max_years",
            "max_reconciliations",
            "reconciliation_interval_days",
        },
    )
    backfill = _exact_table(data, "backfill", {"max_tasks", "minimum_year"})
    return DispatcherConfig(
        daily=DispatchLimits(**daily),
        backfill=BackfillLimits(**backfill),
    )


def parse_utc(value: str) -> datetime:
    """Parse an RFC3339 UTC timestamp and reject offsets/local time."""

    if type(value) is not str or _UTC.search(value) is None:
        raise DueTaskError("UTC-Zeitstempel mit Z erforderlich")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DueTaskError(f"Ungültiger UTC-Zeitstempel: {value}") from exc
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _week_start(value: str) -> date:
    match = _WEEK.fullmatch(value)
    if match is None:
        raise DueTaskError(f"Ungültige ISO-Woche: {value}")
    try:
        return date.fromisocalendar(int(match.group("year")), int(match.group("week")), 1)
    except ValueError as exc:
        raise DueTaskError(f"Nicht existente ISO-Woche: {value}") from exc


def _format_week(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _month_start(value: str) -> date:
    match = _MONTH.fullmatch(value)
    if match is None:
        raise DueTaskError(f"Ungültiger Monat: {value}")
    return date(int(match.group("year")), int(match.group("month")), 1)


def _format_month(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _previous_month(value: date) -> date:
    return date(value.year - (value.month == 1), 12 if value.month == 1 else value.month - 1, 1)


def _validate_period(kind: TaskKind, period: str) -> None:
    if kind is TaskKind.WEEK:
        _week_start(period)
    elif kind is TaskKind.MONTH:
        _month_start(period)
    elif kind is TaskKind.YEAR:
        if type(period) is not str or not re.fullmatch(r"[0-9]{4}", period):
            raise DueTaskError(f"Ungültiges Jahr: {period}")
        year = int(period)
        if not 1900 <= year <= 9999:
            raise DueTaskError(f"Jahr außerhalb des sicheren Bereichs: {period}")
    elif kind is TaskKind.RECONCILIATION:
        parse_utc(period)


def _task(kind: TaskKind, period: str, reason: str, due_at: str) -> DueTask:
    return DueTask(
        task_id=f"{kind.value}:{period}",
        kind=kind,
        period=period,
        reason=reason,
        due_at=due_at,
    )


def _bounded_periods(
    start: date,
    end: date,
    *,
    step,
    formatter,
    maximum: int,
) -> tuple[str, ...]:
    if start > end:
        return ()
    values: list[str] = []
    current = start
    while current <= end and len(values) < maximum:
        values.append(formatter(current))
        current = step(current)
    return tuple(values)


def _status_periods(status: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise DueTaskError("status muss ein JSON-Objekt sein")
    periods = status.get("periods")
    if not isinstance(periods, dict):
        raise DueTaskError("status.periods fehlt")
    required = {
        "last_completed_week",
        "last_completed_month",
        "last_completed_year",
        "last_reconciliation_at",
    }
    if not required <= set(periods):
        raise DueTaskError("status.periods enthält nicht alle Dispatcher-Wasserstände")
    return periods


def calculate_due_tasks(
    status: dict[str, Any],
    now: str,
    limits: DispatchLimits,
) -> tuple[DueTask, ...]:
    """Calculate bounded tasks only for periods that are completely closed."""

    if not isinstance(limits, DispatchLimits):
        raise DueTaskError("limits muss DispatchLimits sein")
    instant = parse_utc(now)
    due_at = _format_utc(instant)
    today = instant.date()
    periods = _status_periods(status)

    current_week_monday = today - timedelta(days=today.weekday())
    last_closed_week = current_week_monday - timedelta(days=7)
    raw_week = periods["last_completed_week"]
    if raw_week is None:
        week_values = (_format_week(last_closed_week),)
    elif type(raw_week) is str:
        completed = _week_start(raw_week)
        if completed > last_closed_week:
            raise DueTaskError("last_completed_week liegt in der Zukunft")
        week_values = _bounded_periods(
            completed + timedelta(days=7),
            last_closed_week,
            step=lambda value: value + timedelta(days=7),
            formatter=_format_week,
            maximum=limits.max_weeks,
        )
    else:
        raise DueTaskError("last_completed_week muss String oder null sein")

    last_closed_month = _previous_month(date(today.year, today.month, 1))
    raw_month = periods["last_completed_month"]
    if raw_month is None:
        month_values = (_format_month(last_closed_month),)
    elif type(raw_month) is str:
        completed_month = _month_start(raw_month)
        if completed_month > last_closed_month:
            raise DueTaskError("last_completed_month liegt in der Zukunft")
        month_values = _bounded_periods(
            _next_month(completed_month),
            last_closed_month,
            step=_next_month,
            formatter=_format_month,
            maximum=limits.max_months,
        )
    else:
        raise DueTaskError("last_completed_month muss String oder null sein")

    last_closed_year = today.year - 1
    raw_year = periods["last_completed_year"]
    if raw_year is None:
        year_values = (str(last_closed_year),)
    elif type(raw_year) is int and not isinstance(raw_year, bool):
        if raw_year > last_closed_year:
            raise DueTaskError("last_completed_year liegt in der Zukunft")
        year_values = tuple(
            str(year)
            for year in range(raw_year + 1, min(last_closed_year, raw_year + limits.max_years) + 1)
        )
    else:
        raise DueTaskError("last_completed_year muss Ganzzahl oder null sein")

    tasks: list[DueTask] = []
    tasks.extend(_task(TaskKind.WEEK, value, "catch_up", due_at) for value in week_values)
    tasks.extend(_task(TaskKind.MONTH, value, "catch_up", due_at) for value in month_values)
    tasks.extend(_task(TaskKind.YEAR, value, "catch_up", due_at) for value in year_values)

    raw_reconciliation = periods["last_reconciliation_at"]
    reconciliation_due = False
    if raw_reconciliation is None:
        reconciliation_due = True
    elif type(raw_reconciliation) is str:
        previous = parse_utc(raw_reconciliation)
        if previous > instant:
            raise DueTaskError("last_reconciliation_at liegt in der Zukunft")
        reconciliation_due = instant - previous >= timedelta(
            days=limits.reconciliation_interval_days
        )
    else:
        raise DueTaskError("last_reconciliation_at muss UTC-String oder null sein")
    if reconciliation_due and limits.max_reconciliations:
        tasks.append(
            _task(TaskKind.RECONCILIATION, due_at, "interval_elapsed", due_at)
        )

    return tuple(
        sorted(tasks, key=lambda item: (_KIND_ORDER[item.kind], item.period, item.task_id))
    )


def calculate_backfill_tasks(
    *,
    from_year: int,
    to_year: int,
    due_at: str,
    limits: BackfillLimits,
) -> tuple[DueTask, ...]:
    """Build a bounded explicit year task set for manual historical work."""

    if type(from_year) is not int or type(to_year) is not int:
        raise DueTaskError("Backfill-Jahre müssen echte Ganzzahlen sein")
    if from_year < limits.minimum_year or to_year > 9999 or from_year > to_year:
        raise DueTaskError("Backfill-Jahresbereich ist ungültig")
    parse_utc(due_at)
    count = to_year - from_year + 1
    if count > limits.max_tasks:
        raise DueTaskError(
            f"Backfill enthält {count} Aufgaben und überschreitet {limits.max_tasks}"
        )
    return tuple(
        _task(TaskKind.YEAR, str(year), "manual_backfill", due_at)
        for year in range(from_year, to_year + 1)
    )


def completed_month_end(period: str) -> date:
    """Return the final date of a validated month; useful for consumers/tests."""

    start = _month_start(period)
    return date(start.year, start.month, monthrange(start.year, start.month)[1])


def task_ids(tasks: Iterable[DueTask]) -> tuple[str, ...]:
    """Return stable unique task IDs or reject duplicates."""

    values = tuple(task.task_id for task in tasks)
    if len(values) != len(set(values)):
        raise DueTaskError("Doppelte task_id")
    return values
