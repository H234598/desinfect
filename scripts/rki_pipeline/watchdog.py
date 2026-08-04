#!/usr/bin/env python3
"""Pure internal-watchdog planning and status projections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.rki_pipeline.due_tasks import DueTaskError, parse_utc
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


class WatchdogError(ValueError):
    """Watchdog input or persisted state is unsafe or inconsistent."""


def _utc(value: str, name: str) -> datetime:
    try:
        return parse_utc(value)
    except DueTaskError as exc:
        raise WatchdogError(f"{name} ist kein gültiger UTC-Zeitstempel") from exc


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _interval(value: object) -> int:
    if type(value) is not int or not 7 <= value <= 55:
        raise WatchdogError("interval_days liegt außerhalb 7..55")
    return value


def _validated_status(status: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise WatchdogError("Status muss ein JSON-Objekt sein")
    watchdog = status.get("watchdog")
    if not isinstance(watchdog, dict):
        raise WatchdogError("Status enthält keinen Watchdog-Zustand")
    _interval(watchdog.get("interval_days"))
    try:
        validate_document("status", status)
    except SchemaContractError as exc:
        raise WatchdogError("Status verletzt den öffentlichen Vertrag") from exc
    return status


@dataclass(frozen=True, slots=True)
class BarkPlan:
    evaluated_at: str
    interval_days: int
    expected_next_bark_at: str
    next_bark_at: str
    causes: tuple[str, ...]
    repeated: bool
    commit_title: str
    commit_body: str

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated_at": self.evaluated_at,
            "interval_days": self.interval_days,
            "expected_next_bark_at": self.expected_next_bark_at,
            "next_bark_at": self.next_bark_at,
            "causes": list(self.causes),
            "repeated": self.repeated,
            "commit_title": self.commit_title,
            "commit_body": self.commit_body,
        }


def _clock_causes(
    status: dict[str, Any], *, now: datetime, interval: int
) -> tuple[str, ...]:
    threshold = now - timedelta(days=interval)
    causes: list[str] = []
    for field, label in (
        ("last_main_commit_at", "last_main_commit"),
        ("last_successful_run_at", "last_successful_run"),
        ("last_successful_write_at", "last_successful_write"),
    ):
        raw = status["pipeline"][field]
        if raw is None:
            causes.append(f"{label}_missing")
            continue
        clock = _utc(raw, field)
        if clock > now:
            raise WatchdogError(f"{field} liegt in der Zukunft")
        if clock < threshold:
            causes.append(f"{label}_stale")
    return tuple(causes) or ("scheduled_keepalive",)


def _commit_text(
    status: dict[str, Any],
    *,
    evaluated_at: str,
    interval: int,
    next_bark_at: str,
    causes: tuple[str, ...],
    repeated: bool,
) -> tuple[str, str]:
    fault = any(
        cause.startswith("last_successful_run")
        or cause.startswith("last_successful_write")
        for cause in causes
    )
    title = (
        f"chore(wachhund): {interval} Tage ohne erfolgreichen Schreiblauf erkannt"
        if fault
        else f"chore(wachhund): neues Betriebsupdate nach {interval} Tagen Inaktivität"
    )
    pipeline = status["pipeline"]
    body = "\n".join(
        (
            "Trigger: internal-watchdog",
            f"Ausgewertet: {evaluated_at}",
            f"Intervall: {interval} Tage",
            f"Letzter main-Commit: {pipeline['last_main_commit_at'] or 'unbekannt'}",
            f"Letzter erfolgreicher Lauf: {pipeline['last_successful_run_at'] or 'unbekannt'}",
            f"Letzter erfolgreicher Schreiblauf: {pipeline['last_successful_write_at'] or 'unbekannt'}",
            f"Ursachen: {','.join(causes)}",
            f"Wiederholung: {'ja' if repeated else 'nein'}",
            f"Nächste Fälligkeit: {next_bark_at}",
        )
    )
    return title, body


def plan_watchdog(status: dict[str, Any], *, as_of: str) -> BarkPlan | None:
    current = _validated_status(status)
    now = _utc(as_of, "as_of")
    evaluated_at = _format_utc(now)
    watchdog = current["watchdog"]
    interval = _interval(watchdog["interval_days"])
    reset_raw = watchdog["last_reset_at"]
    next_raw = watchdog["next_bark_at"]
    if reset_raw is None and next_raw is None:
        return None
    if reset_raw is None or next_raw is None:
        raise WatchdogError("last_reset_at und next_bark_at müssen gemeinsam gesetzt sein")
    reset = _utc(reset_raw, "last_reset_at")
    deadline = _utc(next_raw, "next_bark_at")
    if deadline != reset + timedelta(days=interval):
        raise WatchdogError("next_bark_at stimmt nicht mit Reset und Intervall überein")
    if reset > now or deadline > now:
        if reset > now:
            raise WatchdogError("last_reset_at liegt in der Zukunft")
        return None
    bark_raw = watchdog["last_bark_at"]
    bark = None if bark_raw is None else _utc(bark_raw, "last_bark_at")
    if bark is not None and bark > now:
        raise WatchdogError("last_bark_at liegt in der Zukunft")
    repeated = bark is not None and bark >= reset
    causes = _clock_causes(current, now=now, interval=interval)
    next_bark_at = _format_utc(now + timedelta(days=interval))
    title, body = _commit_text(
        current,
        evaluated_at=evaluated_at,
        interval=interval,
        next_bark_at=next_bark_at,
        causes=causes,
        repeated=repeated,
    )
    return BarkPlan(
        evaluated_at=evaluated_at,
        interval_days=interval,
        expected_next_bark_at=next_raw,
        next_bark_at=next_bark_at,
        causes=causes,
        repeated=repeated,
        commit_title=title,
        commit_body=body,
    )
