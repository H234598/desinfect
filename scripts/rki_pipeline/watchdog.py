#!/usr/bin/env python3
"""Pure internal-watchdog planning and status projections."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

from scripts.rki_pipeline.due_tasks import DueTaskError, parse_utc
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


class WatchdogError(ValueError):
    """Watchdog input or persisted state is unsafe or inconsistent."""


_UTC_DATETIME = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?Z$"
)
_PIPELINE_CLOCKS = (
    ("last_main_commit_at", "last_main_commit"),
    ("last_successful_run_at", "last_successful_run"),
    ("last_successful_write_at", "last_successful_write"),
)


def _utc(value: str, name: str) -> datetime:
    if type(value) is not str or _UTC_DATETIME.fullmatch(value) is None:
        raise WatchdogError(f"{name} ist kein gültiger UTC-Zeitstempel")
    try:
        return parse_utc(value)
    except DueTaskError as exc:
        raise WatchdogError(f"{name} ist kein gültiger UTC-Zeitstempel") from exc


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _shift_days(value: datetime, days: int) -> datetime:
    try:
        return value + timedelta(days=days)
    except OverflowError as exc:
        raise WatchdogError(
            "Watchdog-Zeitberechnung überschreitet den gültigen Datumsbereich"
        ) from exc


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


def _observed_clock(value: object, name: str, *, now: datetime) -> datetime | None:
    if value is None:
        return None
    clock = _utc(value, name)
    if clock > now:
        raise WatchdogError(f"{name} liegt in der Zukunft")
    return clock


def _pipeline_clocks(
    status: dict[str, Any], *, now: datetime
) -> dict[str, datetime | None]:
    return {
        field: _observed_clock(status["pipeline"][field], field, now=now)
        for field, _label in _PIPELINE_CLOCKS
    }


def _clock_causes(
    clocks: dict[str, datetime | None], *, now: datetime, interval: int
) -> tuple[str, ...]:
    threshold = _shift_days(now, -interval)
    causes: list[str] = []
    for field, label in _PIPELINE_CLOCKS:
        clock = clocks[field]
        if clock is None:
            causes.append(f"{label}_missing")
            continue
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
    bark = _observed_clock(watchdog["last_bark_at"], "last_bark_at", now=now)
    pipeline_clocks = _pipeline_clocks(current, now=now)
    reset_raw = watchdog["last_reset_at"]
    next_raw = watchdog["next_bark_at"]
    if reset_raw is None and next_raw is None:
        return None
    if reset_raw is None or next_raw is None:
        raise WatchdogError("last_reset_at und next_bark_at müssen gemeinsam gesetzt sein")
    reset = _utc(reset_raw, "last_reset_at")
    deadline = _utc(next_raw, "next_bark_at")
    repeated = bark is not None and bark >= reset
    anchor = bark if repeated else reset
    if deadline != _shift_days(anchor, interval):
        raise WatchdogError("next_bark_at stimmt nicht mit Reset und Intervall überein")
    if reset > now or deadline > now:
        if reset > now:
            raise WatchdogError("last_reset_at liegt in der Zukunft")
        return None
    causes = _clock_causes(pipeline_clocks, now=now, interval=interval)
    next_bark_at = _format_utc(_shift_days(now, interval))
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


def apply_bark(status: dict[str, Any], plan: BarkPlan) -> dict[str, Any]:
    current = _validated_status(status)
    if not isinstance(plan, BarkPlan):
        raise WatchdogError("Barkplan besitzt den falschen Typ")
    if current["watchdog"]["next_bark_at"] != plan.expected_next_bark_at:
        raise WatchdogError("Barkplan ist veraltet")
    expected = plan_watchdog(current, as_of=plan.evaluated_at)
    if expected != plan:
        raise WatchdogError("Barkplan ist veraltet")
    result = deepcopy(current)
    result["updated_at"] = plan.evaluated_at
    result["watchdog"]["last_bark_at"] = plan.evaluated_at
    result["watchdog"]["next_bark_at"] = plan.next_bark_at
    try:
        validate_document("status", result)
    except SchemaContractError as exc:
        raise WatchdogError("Barkprojektion verletzt den öffentlichen Vertrag") from exc
    return result


def reset_watchdog(
    status: dict[str, Any],
    *,
    now: str,
    interval_days: int = 45,
    reset_by: str,
    run_mode: str,
    run_status: str,
    commit_created: bool,
) -> dict[str, Any]:
    current = _validated_status(status)
    interval = _interval(interval_days)
    if (
        run_mode != "apply"
        or run_status not in {"success", "recovered"}
        or commit_created is not True
    ):
        raise WatchdogError("Nur ein erfolgreicher beabsichtigter Apply-Commit darf zurücksetzen")
    if (
        type(reset_by) is not str
        or not reset_by
        or len(reset_by) > 120
        or not reset_by.isprintable()
    ):
        raise WatchdogError("reset_by muss 1..120 druckbare Zeichen enthalten")
    reset = _utc(now, "now")
    _observed_clock(current["watchdog"]["last_bark_at"], "last_bark_at", now=reset)
    reset_at = _format_utc(reset)
    result = deepcopy(current)
    result["updated_at"] = reset_at
    result["watchdog"]["interval_days"] = interval
    result["watchdog"]["last_reset_at"] = reset_at
    result["watchdog"]["next_bark_at"] = _format_utc(_shift_days(reset, interval))
    result["watchdog"]["reset_by"] = reset_by
    try:
        validate_document("status", result)
    except SchemaContractError as exc:
        raise WatchdogError("Resetprojektion verletzt den öffentlichen Vertrag") from exc
    return result


def _load_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchdogError("Statusdatei ist nicht lesbar") from exc
    if not isinstance(value, dict):
        raise WatchdogError("Status muss ein JSON-Objekt sein")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--mode", choices=("plan",), required=True)
    parser.add_argument("--status", type=Path, default=Path("status.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = _format_utc(_utc(args.as_of, "as_of"))
        plan = plan_watchdog(_load_status(args.status), as_of=as_of)
        payload = {
            "as_of": as_of,
            "bark_plan": None if plan is None else plan.to_dict(),
            "due": plan is not None,
            "mode": args.mode,
            "schema_version": "1.0.0",
        }
        print(stable_json_dumps(payload), end="")
        return 0
    except WatchdogError as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
