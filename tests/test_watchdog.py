from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.watchdog import WatchdogError, plan_watchdog


ROOT = Path(__file__).resolve().parents[1]
RESET = "2026-07-20T04:31:12Z"
DUE = "2026-09-03T04:31:12Z"


def armed_status(
    *,
    interval_days: int = 45,
    last_bark_at: str | None = None,
) -> dict[str, object]:
    value = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    value["watchdog"] = {
        "interval_days": interval_days,
        "last_bark_at": last_bark_at,
        "last_reset_at": RESET,
        "next_bark_at": DUE,
        "reset_by": "weekly_ingest",
    }
    value["pipeline"] = {
        "consecutive_failures": 0,
        "last_error": None,
        "last_main_commit_at": RESET,
        "last_successful_run_at": RESET,
        "last_successful_write_at": RESET,
    }
    return value


def test_unarmed_watchdog_has_no_plan() -> None:
    value = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    assert plan_watchdog(value, as_of=DUE) is None


def test_watchdog_does_not_bark_after_44_days_and_barks_at_deadline() -> None:
    value = armed_status()
    assert plan_watchdog(value, as_of="2026-09-02T04:31:12Z") is None

    plan = plan_watchdog(value, as_of=DUE)

    assert plan is not None
    assert plan.causes == ("scheduled_keepalive",)
    assert plan.next_bark_at == "2026-10-18T04:31:12Z"
    assert plan.repeated is False


def test_delayed_evaluation_emits_one_plan_without_catch_up_bursts() -> None:
    plan = plan_watchdog(armed_status(), as_of="2026-12-01T00:00:00Z")

    assert plan is not None
    assert plan.next_bark_at == "2027-01-15T00:00:00Z"


def test_missing_and_stale_clocks_are_reported_in_stable_order() -> None:
    value = armed_status()
    value["pipeline"]["last_main_commit_at"] = None
    value["pipeline"]["last_successful_run_at"] = "2026-07-19T04:31:12Z"
    value["pipeline"]["last_successful_write_at"] = None

    plan = plan_watchdog(value, as_of=DUE)

    assert plan is not None
    assert plan.causes == (
        "last_main_commit_missing",
        "last_successful_run_stale",
        "last_successful_write_missing",
    )
    assert plan.commit_title == (
        "chore(wachhund): 45 Tage ohne erfolgreichen Schreiblauf erkannt"
    )
    assert "Trigger: internal-watchdog" in plan.commit_body
    assert "Nächste Fälligkeit: 2026-10-18T04:31:12Z" in plan.commit_body


def test_prior_bark_after_reset_marks_repeated_escalation() -> None:
    plan = plan_watchdog(
        armed_status(last_bark_at="2026-08-01T00:00:00Z"),
        as_of=DUE,
    )
    assert plan is not None
    assert plan.repeated is True


@pytest.mark.parametrize("interval_days", [6, 56, True])
def test_interval_outside_safe_range_is_rejected(interval_days: object) -> None:
    value = armed_status()
    value["watchdog"]["interval_days"] = interval_days
    with pytest.raises(WatchdogError, match="7..55"):
        plan_watchdog(value, as_of=DUE)


def test_partial_or_inconsistent_arming_is_rejected() -> None:
    partial = armed_status()
    partial["watchdog"]["last_reset_at"] = None
    with pytest.raises(WatchdogError, match="gemeinsam"):
        plan_watchdog(partial, as_of=DUE)

    inconsistent = armed_status(interval_days=7)
    with pytest.raises(WatchdogError, match="Intervall"):
        plan_watchdog(inconsistent, as_of=DUE)


def test_future_pipeline_clock_is_rejected() -> None:
    value = armed_status()
    value["pipeline"]["last_successful_write_at"] = "2026-09-04T00:00:00Z"
    with pytest.raises(WatchdogError, match="Zukunft"):
        plan_watchdog(value, as_of=DUE)


def test_planning_does_not_mutate_status() -> None:
    value = armed_status()
    before = deepcopy(value)
    plan_watchdog(value, as_of=DUE)
    assert value == before
