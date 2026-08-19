"""Self-heal status/report/scan API routes.

Three endpoints power Task 6's dashboard panel:

- ``GET /api/selfheal/status`` — feature flag + scheduler snapshot. Works with
  the feature fully off (``enabled=False``, ``scheduler=None``): reads
  ``app.state.selfheal_scheduler``, which ``api/app.py`` only sets when
  ``self_heal_enabled`` is true.
- ``GET /api/selfheal/report`` — the latest ``WeeklyReport``, with each
  ``to_fix`` cluster's :class:`~tvastr.selfheal.remediate.FixOutcome` merged
  in by fingerprint when a fix wave ran for that week (via
  ``remediate.load_outcomes``, which reads the additive
  ``{week}-outcomes.json`` file the scheduler's weekly hook now writes next
  to the report). 404s cleanly before the first consolidation ever runs.
- ``POST /api/selfheal/scan`` — manual, synchronous trigger for one daily
  digest or weekly consolidation. Allowed REGARDLESS of ``self_heal_enabled``
  -- this is the demo path; only the background *scheduler* is gated by that
  flag. It never runs the fix wave (that stays scheduler/weekly-hook
  territory), so a manual scan can never open a PR.

Roots default to ``Path("data")``, mirroring ``api/app.py``'s and
``scheduler.py``'s own convention; tests monkeypatch ``default_selfheal_root``
to point at a ``tmp_path`` root.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tvastr.config import get_settings
from tvastr.logging import get_logger
from tvastr.selfheal.remediate import FixOutcome, load_outcomes
from tvastr.selfheal.report import RankedCluster, consolidate_week, load_latest_report, week_key
from tvastr.selfheal.scan import scan_day

log = get_logger(__name__)

router = APIRouter(tags=["selfheal"])

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Disk reads that can raise on a corrupted state file (bad JSON, or valid JSON
# missing the keys a dataclass/model constructor requires). json.JSONDecodeError
# and pydantic's ValidationError both subclass ValueError, so this one tuple
# covers every corrupt-state shape the routes below can hit.
_CORRUPT_STATE_ERRORS = (OSError, ValueError, KeyError, TypeError)


def default_selfheal_root() -> Path:
    """Root data directory for self-heal state (``root/selfheal``,
    ``root/selflogs``, ``root/runs``) -- same ``Path("data")`` convention as
    ``api/app.py`` and ``selfheal/scheduler.py``. Tests monkeypatch this name
    to redirect every route in this module at a ``tmp_path`` root."""
    return Path("data")


def _out_dir(root: Path) -> Path:
    return root / "selfheal"


# ── GET /api/selfheal/status ────────────────────────────────────────────────


class SchedulerStatusOut(BaseModel):
    alive: bool
    last_daily_date: str | None
    last_weekly_week: str | None
    next_expected_daily: str
    next_expected_weekly: str


class SelfHealStatusOut(BaseModel):
    enabled: bool
    scheduler: SchedulerStatusOut | None
    latest_report_week: str | None


@router.get("/api/selfheal/status", response_model=SelfHealStatusOut)
def selfheal_status(request: Request) -> SelfHealStatusOut:
    """Self-heal feature + scheduler snapshot.

    Works fully with the feature off: ``enabled=False`` and ``scheduler=None``
    (``app.state.selfheal_scheduler`` only exists when ``self_heal_enabled``
    is true at startup).
    """
    settings = get_settings()
    scheduler = getattr(request.app.state, "selfheal_scheduler", None)
    out_dir = _out_dir(default_selfheal_root())
    try:
        latest = load_latest_report(out_dir)
    except _CORRUPT_STATE_ERRORS as exc:
        # /status must stay up even when the report on disk is corrupt -- it's
        # the liveness/health surface, not a place a bad file should 500.
        # Degrade to "no report" rather than failing the whole snapshot.
        log.warning("selfheal.api.status_report_unreadable", out_dir=str(out_dir), error=str(exc))
        latest = None
    return SelfHealStatusOut(
        enabled=settings.self_heal_enabled,
        scheduler=SchedulerStatusOut(**scheduler.status()) if scheduler is not None else None,
        latest_report_week=latest.week if latest is not None else None,
    )


# ── GET /api/selfheal/report ────────────────────────────────────────────────


class FixOutcomeOut(BaseModel):
    run_id: str | None
    status: str
    pr_url: str | None
    pr_urls: tuple[str, ...] = ()


class RankedClusterOut(BaseModel):
    fingerprint: str
    title: str
    count: int
    severity_weight: float
    score: float
    kind: str
    sample_messages: list[str]
    first_seen: str
    last_seen: str
    # Only ever populated for a `to_fix` cluster whose fingerprint appears in
    # the week's persisted outcomes -- `report_only` clusters are never
    # attempted by the wave, so their outcome is always None.
    outcome: FixOutcomeOut | None = None


class WeeklyReportOut(BaseModel):
    week: str
    generated_at: str
    to_fix: list[RankedClusterOut]
    report_only: list[RankedClusterOut]
    days_scanned: list[str]


def _cluster_out(cluster: RankedCluster, outcome_by_fp: dict[str, FixOutcome]) -> RankedClusterOut:
    outcome = outcome_by_fp.get(cluster.fingerprint)
    return RankedClusterOut(
        **asdict(cluster),
        outcome=FixOutcomeOut(**asdict(outcome)) if outcome is not None else None,
    )


@router.get("/api/selfheal/report", response_model=WeeklyReportOut)
def selfheal_report() -> WeeklyReportOut:
    """Latest weekly self-heal report, with fix-wave outcomes merged in.

    404 ``{"detail": "no weekly report yet"}`` before the first consolidation
    has ever run.
    """
    out_dir = _out_dir(default_selfheal_root())
    try:
        report = load_latest_report(out_dir)
    except _CORRUPT_STATE_ERRORS as exc:
        log.warning("selfheal.api.report_unreadable", out_dir=str(out_dir), error=str(exc))
        raise HTTPException(500, f"self-heal report on disk is unreadable: {exc}") from exc
    if report is None:
        raise HTTPException(404, "no weekly report yet")

    outcomes = load_outcomes(report.week, out_dir) or []
    outcome_by_fp = {o.fingerprint: o for o in outcomes}

    return WeeklyReportOut(
        week=report.week,
        generated_at=report.generated_at,
        # report_only clusters are never fix-wave candidates, so pass an empty
        # outcome map for them even if a same-fingerprint outcome existed in a
        # PRIOR week's file -- an outcome only ever applies to its own week.
        to_fix=[_cluster_out(c, outcome_by_fp) for c in report.to_fix],
        report_only=[_cluster_out(c, {}) for c in report.report_only],
        days_scanned=report.days_scanned,
    )


# ── POST /api/selfheal/scan ─────────────────────────────────────────────────


class ScanRequest(BaseModel):
    kind: Literal["daily", "weekly"]
    day: str | None = None  # YYYY-MM-DD; default: yesterday UTC
    week: str | None = None  # YYYY-Www; default: the completed previous ISO week


def _yesterday_utc() -> str:
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def _previous_iso_week() -> str:
    return week_key((datetime.now(UTC).date() - timedelta(days=7)).isoformat())


@router.post("/api/selfheal/scan", response_model=None)
def selfheal_scan(body: ScanRequest) -> dict[str, Any]:
    """Manually run one self-heal stage synchronously; return its summary.

    Allowed REGARDLESS of ``self_heal_enabled`` -- this is the demo path; only
    the background *scheduler* (``SelfHealScheduler``) is gated by that flag.
    ``kind="daily"`` runs ``scan.scan_day`` for ``day`` (default: yesterday
    UTC). ``kind="weekly"`` runs ``report.consolidate_week`` for ``week``
    (default: the completed previous ISO week, ``week_key(today - 7 days)``)
    using the configured ``self_heal_top_n``/``self_heal_fix_n``. This never
    runs the fix wave -- a manual scan must never open a PR.
    """
    settings = get_settings()
    root = default_selfheal_root()
    out_dir = _out_dir(root)

    if body.kind == "daily":
        day = body.day or _yesterday_utc()
        if not _DAY_RE.match(day):
            raise HTTPException(400, f"invalid day {day!r}, expected YYYY-MM-DD")
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            # Shape-valid (YYYY-MM-DD) but not a real calendar date, e.g.
            # "2026-02-31" -- distinct from the 400 above, which is a bare
            # shape mismatch.
            raise HTTPException(422, f"invalid day {day!r}: not a real date") from exc
        digest = scan_day(
            day,
            selflogs_dir=root / "selflogs",
            runs_dir=root / "runs",
            out_dir=out_dir,
        )
        log.info("selfheal.api.scan_daily", day=digest.day, cluster_count=len(digest.clusters))
        return {
            "kind": "daily",
            "day": digest.day,
            "cluster_count": len(digest.clusters),
            "event_count": len(digest.events),
            "scanned_runs": digest.scanned_runs,
            "skipped_self_runs": digest.skipped_self_runs,
        }

    week = body.week or _previous_iso_week()
    if not _WEEK_RE.match(week):
        raise HTTPException(400, f"invalid week {week!r}, expected YYYY-Www")
    year_str, week_str = week.split("-W")
    try:
        date.fromisocalendar(int(year_str), int(week_str), 1)
    except ValueError as exc:
        # Shape-valid (YYYY-Www) but out of ISO-calendar range, e.g.
        # "2026-W99" -- 2026 tops out at week 53.
        raise HTTPException(422, f"invalid ISO week {week!r}: {exc}") from exc
    try:
        report = consolidate_week(
            week,
            digests_dir=out_dir,
            out_dir=out_dir,
            top_n=settings.self_heal_top_n,
            fix_n=settings.self_heal_fix_n,
        )
    except _CORRUPT_STATE_ERRORS as exc:
        log.warning("selfheal.api.scan_weekly_unreadable", week=week, error=str(exc))
        raise HTTPException(500, f"self-heal daily digest on disk is unreadable: {exc}") from exc
    log.info(
        "selfheal.api.scan_weekly",
        week=report.week,
        to_fix=len(report.to_fix),
        report_only=len(report.report_only),
    )
    return {"kind": "weekly", **asdict(report)}
