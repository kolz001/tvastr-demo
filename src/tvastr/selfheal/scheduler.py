"""Scheduler thread: fires the daily scan and weekly consolidation on a cadence,
idempotent across restarts via a small on-disk state file.

``tick()`` is the ONE testable decision function -- given "now" (injected via
``now_fn``) and the persisted :class:`SchedulerState`, it decides whether a
daily catch-up and/or a weekly consolidation are due, and if so fires them.
Every test in ``test_selfheal_scheduler.py`` drives ``tick()`` directly with a
fake clock and a ``tmp_path`` root; nothing sleeps.

Two invariants matter more than anything else here:

1. **State is saved BEFORE the hook runs.** A crashing daily/weekly job must
   never re-fire forever -- once ``tick()`` has decided "today's daily is
   2026-08-18", that decision is durable even if the hook itself blows up.
2. **Catch-up is bounded to "yesterday" (daily) / "the previous completed ISO
   week" (weekly), never an arbitrary backfill.** If the process was down for
   a week, the scheduler does not try to replay every missed day -- it simply
   resumes from "yesterday" on the next tick. This mirrors ``scan.py``'s and
   ``report.py``'s own single-period-per-call design.

The default hooks wrap Task 2/3's pure functions (``scan.scan_day``,
``report.consolidate_week``); Task 5 swaps the weekly hook for a richer
callable (an actual fix-wave), which is why ``run_daily``/``run_weekly`` are
injectable ``Callable[[str], None]`` parameters rather than hardwired calls.

``self_heal_weekly_day`` uses Python's ``date.isoweekday()`` convention
(1=Monday .. 7=Sunday) -- see the ``Settings.self_heal_weekly_day`` docstring
in ``config.py`` for the resolved spec-vs-code note.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tvastr.config import Settings
from tvastr.logging import get_logger
from tvastr.selfheal.report import consolidate_week, week_key
from tvastr.selfheal.scan import scan_day

log = get_logger(__name__)

# How often the background loop re-evaluates ``tick()`` between firings.
# ``Event.wait(_POLL_SECONDS)`` (not ``time.sleep``) so ``stop()`` interrupts
# the wait promptly instead of blocking for the full interval.
_POLL_SECONDS = 300

_STATE_FILENAME = "scheduler_state.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SchedulerState:
    """Persisted idempotency record: the last day/week each job fired for.

    ``None`` means "never fired". A missing or corrupt state file loads as an
    empty state (both fields ``None``) rather than raising -- a scheduler
    starting for the first time, or recovering from a truncated write, should
    behave exactly like a fresh install, not crash.
    """

    last_daily_date: str | None = None
    last_weekly_week: str | None = None

    @classmethod
    def load(cls, path: Path) -> SchedulerState:
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(payload, dict):
            return cls()
        last_daily = payload.get("last_daily_date")
        last_weekly = payload.get("last_weekly_week")
        return cls(
            last_daily_date=last_daily if isinstance(last_daily, str) else None,
            last_weekly_week=last_weekly if isinstance(last_weekly, str) else None,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")


class SelfHealScheduler:
    """Daemon-thread scheduler for the self-heal daily scan + weekly consolidation.

    Construct once, call :meth:`start` to launch the background thread (which
    calls :meth:`tick` immediately, then every ``_POLL_SECONDS`` until
    :meth:`stop`), or call :meth:`tick` directly in tests with an injected
    ``now_fn``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        root: Path = Path("data"),
        now_fn: Callable[[], datetime] = _utcnow,
        run_daily: Callable[[str], None] | None = None,
        run_weekly: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._root = root
        self._now_fn = now_fn
        self._run_daily = run_daily or self._default_daily
        self._run_weekly = run_weekly or self._default_weekly

        self._selfheal_dir = root / "selfheal"
        self._state_path = self._selfheal_dir / _STATE_FILENAME
        self._state = SchedulerState.load(self._state_path)

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- default hooks (Task 2/3 functions; Task 5 swaps run_weekly) --------

    def _default_daily(self, day: str) -> None:
        scan_day(
            day,
            selflogs_dir=self._root / "selflogs",
            runs_dir=self._root / "runs",
            out_dir=self._selfheal_dir,
        )

    def _default_weekly(self, week: str) -> None:
        consolidate_week(
            week,
            digests_dir=self._selfheal_dir,
            out_dir=self._selfheal_dir,
            top_n=self._settings.self_heal_top_n,
            fix_n=self._settings.self_heal_fix_n,
        )

    # -- the one testable decision function ----------------------------------

    def tick(self) -> list[str]:
        """Fire at most one catch-up daily and one weekly job, if due.

        Returns labels of what fired, e.g. ``["daily:2026-08-18",
        "weekly:2026-W34"]``. Never raises: hook exceptions are caught and
        logged, not propagated.
        """
        fired: list[str] = []
        now = self._now_fn()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        hour_reached = now.hour >= self._settings.self_heal_daily_hour

        if hour_reached and self._state.last_daily_date != yesterday:
            fired.append(self._fire_daily(yesterday))

        if hour_reached and now.isoweekday() == self._settings.self_heal_weekly_day:
            target_week = week_key(yesterday)
            if self._state.last_weekly_week != target_week:
                fired.append(self._fire_weekly(target_week))

        return fired

    def _fire_daily(self, day: str) -> str:
        # Save BEFORE running: a crashing hook must never cause a re-fire.
        self._state.last_daily_date = day
        self._save_state()
        try:
            self._run_daily(day)
        except Exception as exc:  # must never propagate out of tick()
            log.error("selfheal.scheduler.daily_failed", day=day, error=str(exc))
        return f"daily:{day}"

    def _fire_weekly(self, week: str) -> str:
        self._state.last_weekly_week = week
        self._save_state()
        try:
            self._run_weekly(week)
        except Exception as exc:  # must never propagate out of tick()
            log.error("selfheal.scheduler.weekly_failed", week=week, error=str(exc))
        return f"weekly:{week}"

    def _save_state(self) -> None:
        self._state.save(self._state_path)

    # -- thread lifecycle ------------------------------------------------------

    def start(self) -> threading.Thread:
        """Start the daemon background thread. Idempotent-ish: calling twice
        replaces the tracked thread, but does not stop a previously started one
        -- callers (``app.py``) are expected to call this at most once."""
        self._stop_event.clear()
        thread = threading.Thread(target=self._loop, name="selfheal-scheduler", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # the loop must survive anything
                log.exception("selfheal.scheduler.tick_failed")
            # Event.wait (not time.sleep) so stop() interrupts promptly instead
            # of blocking for the full poll interval.
            if self._stop_event.wait(_POLL_SECONDS):
                break

    # -- status for the API ---------------------------------------------------

    def status(self) -> dict[str, bool | str | None]:
        """JSON-safe status snapshot: alive, last-fired state, and what the
        next tick would target if it ran right now. Task 6 serves this
        verbatim as the status route's response body."""
        now = self._now_fn()
        yesterday = (now.date() - timedelta(days=1)).isoformat()

        if self._state.last_daily_date != yesterday:
            next_daily = yesterday
        else:
            next_daily = (date.fromisoformat(yesterday) + timedelta(days=1)).isoformat()

        target_week = week_key(yesterday)
        if self._state.last_weekly_week != target_week:
            next_weekly = target_week
        else:
            next_weekly = week_key((date.fromisoformat(yesterday) + timedelta(days=7)).isoformat())

        return {
            "alive": self._thread is not None and self._thread.is_alive(),
            "last_daily_date": self._state.last_daily_date,
            "last_weekly_week": self._state.last_weekly_week,
            "next_expected_daily": next_daily,
            "next_expected_weekly": next_weekly,
        }
