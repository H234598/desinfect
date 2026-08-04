from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import time

import pytest

from scripts.rki_pipeline.cli import main as pipeline_main
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.watchdog import (
    WatchdogError,
    apply_bark,
    main as watchdog_main,
    plan_watchdog,
    reset_watchdog,
)


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


@pytest.mark.parametrize("host_tz", ["UTC", "Europe/Berlin"])
def test_date_only_as_of_is_rejected_independently_of_host_timezone(
    host_tz: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", host_tz)
        time.tzset()

        with pytest.raises(WatchdogError, match="UTC-Zeitstempel"):
            plan_watchdog(armed_status(), as_of="2026-09-04Z")
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()


def test_watchdog_does_not_bark_after_44_days_and_barks_at_deadline() -> None:
    value = armed_status()
    assert plan_watchdog(value, as_of="2026-09-02T04:31:12Z") is None

    plan = plan_watchdog(value, as_of=DUE)

    assert plan is not None
    assert plan.causes == ("scheduled_keepalive",)
    assert plan.next_bark_at == "2026-10-18T04:31:12Z"
    assert plan.repeated is False


def test_applied_bark_schedules_repeated_bark_one_interval_later() -> None:
    value = armed_status()
    first_plan = plan_watchdog(value, as_of=DUE)
    assert first_plan is not None
    applied = apply_bark(value, first_plan)

    second_plan = plan_watchdog(applied, as_of=first_plan.next_bark_at)

    assert second_plan is not None
    assert second_plan.repeated is True
    assert second_plan.next_bark_at == "2026-12-02T04:31:12Z"


def test_fractional_due_plan_round_trips_through_apply() -> None:
    due = "2026-09-03T04:31:12.500000Z"
    value = armed_status()
    value["watchdog"]["last_reset_at"] = "2026-07-20T04:31:12.500000Z"
    value["watchdog"]["next_bark_at"] = due
    for field in (
        "last_main_commit_at",
        "last_successful_run_at",
        "last_successful_write_at",
    ):
        value["pipeline"][field] = "2026-07-20T04:31:12.500000Z"

    plan = plan_watchdog(value, as_of=due)

    assert plan is not None
    assert plan.evaluated_at == due
    assert plan.next_bark_at == "2026-10-18T04:31:12.500000Z"
    projected = apply_bark(value, plan)
    assert projected["updated_at"] == due
    assert projected["watchdog"]["last_bark_at"] == due


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
    value = armed_status(last_bark_at="2026-08-01T00:00:00Z")
    value["watchdog"]["next_bark_at"] = "2026-09-15T00:00:00Z"

    plan = plan_watchdog(value, as_of="2026-09-15T00:00:00Z")

    assert plan is not None
    assert plan.repeated is True


@pytest.mark.parametrize("interval_days", [6, 56, True])
def test_interval_outside_safe_range_is_rejected(interval_days: object) -> None:
    value = armed_status()
    value["watchdog"]["interval_days"] = interval_days
    with pytest.raises(WatchdogError, match=r"7\.\.55"):
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


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("watchdog", "last_bark_at"),
        ("pipeline", "last_main_commit_at"),
        ("pipeline", "last_successful_run_at"),
        ("pipeline", "last_successful_write_at"),
    ],
)
def test_unarmed_watchdog_rejects_future_observed_clocks(
    section: str,
    field: str,
) -> None:
    value = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    value[section][field] = "2026-09-04T00:00:00Z"

    with pytest.raises(WatchdogError, match=f"{field} liegt in der Zukunft"):
        plan_watchdog(value, as_of=DUE)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("watchdog", "last_bark_at"),
        ("pipeline", "last_main_commit_at"),
        ("pipeline", "last_successful_run_at"),
        ("pipeline", "last_successful_write_at"),
    ],
)
def test_pre_deadline_watchdog_rejects_future_observed_clocks(
    section: str,
    field: str,
) -> None:
    value = armed_status()
    value[section][field] = "2026-09-04T00:00:00Z"

    with pytest.raises(WatchdogError, match=f"{field} liegt in der Zukunft"):
        plan_watchdog(value, as_of="2026-09-02T04:31:12Z")


def test_planning_does_not_mutate_status() -> None:
    value = armed_status()
    before = deepcopy(value)
    plan_watchdog(value, as_of=DUE)
    assert value == before


def test_bark_projection_advances_only_watchdog_clocks() -> None:
    value = armed_status()
    before = deepcopy(value)
    plan = plan_watchdog(value, as_of=DUE)
    assert plan is not None

    projected = apply_bark(value, plan)

    assert value == before
    assert projected["pipeline"] == before["pipeline"]
    assert projected["watchdog"]["last_bark_at"] == DUE
    assert projected["watchdog"]["last_reset_at"] == RESET
    assert projected["watchdog"]["next_bark_at"] == "2026-10-18T04:31:12Z"
    assert projected["updated_at"] == DUE


def test_bark_plan_cannot_be_replayed_or_applied_to_changed_state() -> None:
    value = armed_status()
    plan = plan_watchdog(value, as_of=DUE)
    assert plan is not None
    applied = apply_bark(value, plan)

    with pytest.raises(WatchdogError, match="veraltet"):
        apply_bark(applied, plan)

    changed = armed_status()
    changed["pipeline"]["last_successful_write_at"] = None
    with pytest.raises(WatchdogError, match="veraltet"):
        apply_bark(changed, plan)


def test_successful_apply_commit_resets_without_touching_pipeline_clocks() -> None:
    value = armed_status(last_bark_at=DUE)
    before = deepcopy(value)

    projected = reset_watchdog(
        value,
        now="2026-09-04T00:00:00Z",
        interval_days=45,
        reset_by="weekly_ingest",
        run_mode="apply",
        run_status="success",
        commit_created=True,
    )

    assert value == before
    assert projected["pipeline"] == before["pipeline"]
    assert projected["watchdog"] == {
        "interval_days": 45,
        "last_bark_at": DUE,
        "last_reset_at": "2026-09-04T00:00:00Z",
        "next_bark_at": "2026-10-19T00:00:00Z",
        "reset_by": "weekly_ingest",
    }
    assert projected["updated_at"] == "2026-09-04T00:00:00Z"


def test_reset_rejects_time_before_existing_bark() -> None:
    value = armed_status(last_bark_at="2026-09-05T00:00:00Z")

    with pytest.raises(WatchdogError, match="last_bark_at liegt in der Zukunft"):
        reset_watchdog(
            value,
            now="2026-09-04T00:00:00Z",
            reset_by="weekly_ingest",
            run_mode="apply",
            run_status="success",
            commit_created=True,
        )


@pytest.mark.parametrize(
    ("run_mode", "run_status", "commit_created"),
    [
        ("plan", "success", True),
        ("materialize", "success", True),
        ("apply", "no_op", True),
        ("apply", "failed", True),
        ("apply", "success", False),
    ],
)
def test_ineligible_run_cannot_reset_watchdog(
    run_mode: str,
    run_status: str,
    commit_created: bool,
) -> None:
    with pytest.raises(WatchdogError, match="Apply-Commit"):
        reset_watchdog(
            armed_status(),
            now="2026-09-04T00:00:00Z",
            reset_by="weekly_ingest",
            run_mode=run_mode,
            run_status=run_status,
            commit_created=commit_created,
        )


@pytest.mark.parametrize("interval_days", [7, 55])
def test_reset_accepts_safe_interval_boundaries(interval_days: int) -> None:
    projected = reset_watchdog(
        armed_status(),
        now="2026-09-04T00:00:00Z",
        interval_days=interval_days,
        reset_by="weekly_ingest",
        run_mode="apply",
        run_status="recovered",
        commit_created=True,
    )
    assert projected["watchdog"]["interval_days"] == interval_days


def test_reset_reports_upper_datetime_overflow_as_watchdog_error() -> None:
    with pytest.raises(WatchdogError, match="Zeitberechnung"):
        reset_watchdog(
            armed_status(),
            now="9999-12-31T00:00:00Z",
            reset_by="weekly_ingest",
            run_mode="apply",
            run_status="success",
            commit_created=True,
        )


@pytest.mark.parametrize("reset_by", ["", "x" * 121, "line\nbreak"])
def test_reset_reason_must_be_bounded_and_printable(reset_by: str) -> None:
    with pytest.raises(WatchdogError, match="reset_by"):
        reset_watchdog(
            armed_status(),
            now="2026-09-04T00:00:00Z",
            reset_by=reset_by,
            run_mode="apply",
            run_status="success",
            commit_created=True,
        )


def test_watchdog_cli_is_read_only_and_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    original = stable_json_dumps(armed_status())
    status_path.write_text(original, encoding="utf-8")
    arguments = ["--as-of", DUE, "--mode", "plan", "--status", str(status_path)]

    assert watchdog_main(arguments) == 0
    first = capsys.readouterr().out
    assert watchdog_main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    payload = json.loads(first)
    assert payload["due"] is True
    assert payload["bark_plan"]["expected_next_bark_at"] == DUE
    assert status_path.read_text(encoding="utf-8") == original


def test_domain_router_runs_watchdog_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(stable_json_dumps(armed_status()), encoding="utf-8")

    result = pipeline_main(
        ["watchdog", "--as-of", DUE, "--mode", "plan", "--status", str(status_path)]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["due"] is True


def test_watchdog_cli_reports_invalid_status_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text("{}\n", encoding="utf-8")

    result = watchdog_main(
        ["--as-of", DUE, "--mode", "plan", "--status", str(status_path)]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("watchdog:")
    assert "Traceback" not in captured.err


def test_watchdog_cli_reports_invalid_utf8_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_bytes(b"\xff")

    result = watchdog_main(
        ["--as-of", DUE, "--mode", "plan", "--status", str(status_path)]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("watchdog:")
    assert "Traceback" not in captured.err


def test_watchdog_cli_reports_upper_datetime_overflow_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    value = armed_status()
    value["watchdog"]["last_reset_at"] = "9999-10-01T00:00:00Z"
    value["watchdog"]["next_bark_at"] = "9999-11-15T00:00:00Z"
    for field in (
        "last_main_commit_at",
        "last_successful_run_at",
        "last_successful_write_at",
    ):
        value["pipeline"][field] = "9999-12-01T00:00:00Z"
    status_path.write_text(stable_json_dumps(value), encoding="utf-8")

    result = watchdog_main(
        [
            "--as-of",
            "9999-12-31T00:00:00Z",
            "--mode",
            "plan",
            "--status",
            str(status_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("watchdog:")
    assert "Traceback" not in captured.err
