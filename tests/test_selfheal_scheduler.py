"""Tests for the self-heal scheduler (scheduler.py).

Everything goes through ``tick()`` with an injected ``now_fn`` and a ``tmp_path``
state/data root -- no ``time.sleep``, no real clock. Week 2026-W34 runs Monday
2026-08-17 through Sunday 2026-08-23 (see test_selfheal_report.py).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from tvastr.config import Settings
from tvastr.selfheal.scheduler import SchedulerState, SelfHealScheduler

WED_BEFORE_HOUR = datetime(2026, 8, 19, 1, 30, tzinfo=UTC)
WED_AT_HOUR = datetime(2026, 8, 19, 2, 0, tzinfo=UTC)
SUN_AT_HOUR = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "self_heal_daily_hour": 2,
        "self_heal_weekly_day": 7,  # isoweekday: Sunday
        "self_heal_top_n": 10,
        "self_heal_fix_n": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _no_op(_: str) -> None:
    return None


# ── tick(): daily catch-up ────────────────────────────────────────────────


def test_daily_fires_yesterday_exactly_once_when_hour_reached(tmp_path: Path) -> None:
    calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )

    fired = scheduler.tick()

    assert fired == ["daily:2026-08-18"]
    assert calls == ["2026-08-18"]


def test_daily_fires_nothing_before_the_hour(tmp_path: Path) -> None:
    calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_BEFORE_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )

    fired = scheduler.tick()

    assert fired == []
    assert calls == []


def test_second_tick_same_day_fires_nothing(tmp_path: Path) -> None:
    calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )

    first = scheduler.tick()
    second = scheduler.tick()

    assert first == ["daily:2026-08-18"]
    assert second == []
    assert calls == ["2026-08-18"]


def test_restart_with_same_state_file_fires_nothing(tmp_path: Path) -> None:
    calls: list[str] = []
    scheduler1 = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )
    scheduler1.tick()

    # A fresh scheduler instance (simulating a process restart) reading the
    # same on-disk state file must not refire the day already recorded.
    scheduler2 = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )
    fired = scheduler2.tick()

    assert fired == []
    assert calls == ["2026-08-18"]


def test_no_backfill_beyond_yesterday(tmp_path: Path) -> None:
    """A state file stale by many days only ever catches up on yesterday, never
    the arbitrary gap in between."""
    state_path = tmp_path / "selfheal" / "scheduler_state.json"
    SchedulerState(last_daily_date="2026-08-01", last_weekly_week=None).save(state_path)
    calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=calls.append,
        run_weekly=_no_op,
    )

    fired = scheduler.tick()

    assert fired == ["daily:2026-08-18"]
    assert calls == ["2026-08-18"]


# ── tick(): weekly consolidation ──────────────────────────────────────────


def test_weekly_fires_for_completed_previous_week_on_configured_day(tmp_path: Path) -> None:
    daily_calls: list[str] = []
    weekly_calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: SUN_AT_HOUR,
        run_daily=daily_calls.append,
        run_weekly=weekly_calls.append,
    )

    fired = scheduler.tick()

    assert fired == ["daily:2026-08-22", "weekly:2026-W34"]
    assert weekly_calls == ["2026-W34"]


def test_weekly_does_not_fire_on_non_configured_weekday(tmp_path: Path) -> None:
    weekly_calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=_no_op,
        run_weekly=weekly_calls.append,
    )

    scheduler.tick()

    assert weekly_calls == []


def test_weekly_second_tick_same_week_fires_nothing(tmp_path: Path) -> None:
    weekly_calls: list[str] = []
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: SUN_AT_HOUR,
        run_daily=_no_op,
        run_weekly=weekly_calls.append,
    )

    scheduler.tick()
    second = scheduler.tick()

    assert weekly_calls == ["2026-W34"]
    assert "weekly:2026-W34" not in second


# ── tick(): hook exceptions ────────────────────────────────────────────────


def test_daily_hook_exception_still_records_state_and_does_not_propagate(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def _raising(day: str) -> None:
        calls.append(day)
        raise RuntimeError("boom")

    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=_raising,
        run_weekly=_no_op,
    )

    fired = scheduler.tick()  # must not raise

    assert fired == ["daily:2026-08-18"]
    assert calls == ["2026-08-18"]

    # State was saved BEFORE the raising hook ran, so a second tick the same
    # day must not refire it.
    second = scheduler.tick()
    assert second == []
    assert calls == ["2026-08-18"]


def test_weekly_hook_exception_still_records_state_and_does_not_propagate(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def _raising(week: str) -> None:
        calls.append(week)
        raise RuntimeError("boom")

    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: SUN_AT_HOUR,
        run_daily=_no_op,
        run_weekly=_raising,
    )

    fired = scheduler.tick()  # must not raise

    assert "weekly:2026-W34" in fired
    assert calls == ["2026-W34"]

    second = scheduler.tick()
    assert "weekly:2026-W34" not in second
    assert calls == ["2026-W34"]


# ── SchedulerState ─────────────────────────────────────────────────────────


def test_scheduler_state_missing_file_loads_empty(tmp_path: Path) -> None:
    state = SchedulerState.load(tmp_path / "nope.json")
    assert state == SchedulerState(last_daily_date=None, last_weekly_week=None)


def test_scheduler_state_corrupt_file_loads_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json{{{", encoding="utf-8")
    state = SchedulerState.load(path)
    assert state == SchedulerState(last_daily_date=None, last_weekly_week=None)


def test_scheduler_state_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "state.json"
    original = SchedulerState(last_daily_date="2026-08-18", last_weekly_week="2026-W33")
    original.save(path)

    loaded = SchedulerState.load(path)

    assert loaded == original
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"last_daily_date": "2026-08-18", "last_weekly_week": "2026-W33"}


# ── status() ────────────────────────────────────────────────────────────


def test_status_reflects_state_before_start(tmp_path: Path) -> None:
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=_no_op,
        run_weekly=_no_op,
    )

    scheduler.tick()
    status = scheduler.status()

    assert status["alive"] is False
    assert status["last_daily_date"] == "2026-08-18"
    assert status["last_weekly_week"] is None
    assert isinstance(status["next_expected_daily"], str)
    assert isinstance(status["next_expected_weekly"], str)
    # JSON-safe: every value round-trips through json.dumps without error.
    json.dumps(status)


def test_status_next_expected_daily_advances_once_recorded(tmp_path: Path) -> None:
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=_no_op,
        run_weekly=_no_op,
    )
    before = scheduler.status()["next_expected_daily"]
    scheduler.tick()
    after = scheduler.status()["next_expected_daily"]

    assert before == "2026-08-18"
    assert after == "2026-08-19"


# ── start()/stop() thread lifecycle ────────────────────────────────────────


def test_start_stop_thread_lifecycle(tmp_path: Path) -> None:
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_BEFORE_HOUR,  # before the hour: tick() is a no-op
        run_daily=_no_op,
        run_weekly=_no_op,
    )

    thread = scheduler.start()
    assert thread.is_alive()
    assert thread.daemon is True
    status_while_alive = scheduler.status()
    assert status_while_alive["alive"] is True

    scheduler.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert scheduler.status()["alive"] is False


def test_thread_loop_catches_hook_exceptions_and_keeps_running(tmp_path: Path) -> None:
    def _raising(day: str) -> None:
        raise RuntimeError("boom")

    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
        run_daily=_raising,
        run_weekly=_no_op,
    )

    thread = scheduler.start()
    # The first loop iteration ticks immediately (hook raises internally,
    # tick() swallows it); the thread must still be alive afterwards.
    for _ in range(50):
        if scheduler.status()["last_daily_date"] == "2026-08-18":
            break
        threading.Event().wait(0.01)
    assert thread.is_alive()
    scheduler.stop()
    thread.join(timeout=5)


# ── default hooks wrap scan.scan_day / report.consolidate_week ────────────


def test_default_daily_hook_wraps_scan_day(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    selflogs_dir.mkdir()
    (selflogs_dir / "tvastr-2026-08-18.jsonl").write_text(
        json.dumps({"event": "boom", "level": "error", "timestamp": "2026-08-18T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: WED_AT_HOUR,
    )

    scheduler.tick()

    digest_path = tmp_path / "selfheal" / "daily" / "2026-08-18.jsonl"
    assert digest_path.exists()


def test_default_weekly_hook_wraps_consolidate_week(tmp_path: Path) -> None:
    scheduler = SelfHealScheduler(
        settings=_settings(),
        root=tmp_path,
        now_fn=lambda: SUN_AT_HOUR,
    )

    scheduler.tick()

    report_path = tmp_path / "selfheal" / "weekly" / "2026-W34.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["week"] == "2026-W34"


# ── app wiring: zero threads under the conftest seal ───────────────────────


def test_testclient_under_conftest_seal_starts_zero_threads() -> None:
    """TVASTR_SELF_HEAL_ENABLED=false is sealed in conftest.py -- building
    TestClient(create_app()) must not start the scheduler thread."""
    from fastapi.testclient import TestClient

    from tvastr.api.app import create_app

    before = {t.name for t in threading.enumerate()}
    with TestClient(create_app()):
        after = {t.name for t in threading.enumerate()}
    new_threads = after - before
    selfheal_threads = {name for name in new_threads if "selfheal" in name.lower()}
    assert selfheal_threads == set()
