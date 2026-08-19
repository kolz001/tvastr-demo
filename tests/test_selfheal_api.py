"""Tests for the self-heal API routes (api/routes/selfheal.py).

Every test that touches disk redirects ``default_selfheal_root`` (the route
module's own root-directory hook, mirroring ``events.default_runs_dir``'s
monkeypatch pattern in ``test_run_lifecycle.py``) at a ``tmp_path`` root —
never the real ``data/`` on this machine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tvastr.api import create_app
from tvastr.api.routes import selfheal as selfheal_routes
from tvastr.config import Settings
from tvastr.selfheal import remediate as remediate_mod
from tvastr.selfheal.remediate import FixOutcome, write_outcomes
from tvastr.selfheal.report import consolidate_week, week_key
from tvastr.selfheal.scan import scan_day
from tvastr.selfheal.selflog import selflog_path

client = TestClient(create_app())


def _redirect_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(selfheal_routes, "default_selfheal_root", lambda: root)


def _write_selflog(selflogs_dir: Path, day: str, records: list[dict]) -> None:
    path = selflog_path(selflogs_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _error_record(message: str, day: str) -> dict:
    return {
        "event": message,
        "level": "error",
        "timestamp": f"{day}T10:00:00+00:00",
        "logger": "tvastr.pipeline",
    }


# ── router registration ─────────────────────────────────────────────────────


def test_router_registered_in_create_app() -> None:
    paths = {getattr(route, "path", None) for route in create_app().routes}
    assert "/api/selfheal/status" in paths
    assert "/api/selfheal/report" in paths
    assert "/api/selfheal/scan" in paths


# ── GET /api/selfheal/status ────────────────────────────────────────────────


def test_status_disabled_by_default() -> None:
    """TVASTR_SELF_HEAL_ENABLED=false is sealed in conftest.py -- the default
    TestClient must report enabled=False and scheduler=None, never 500."""
    resp = client.get("/api/selfheal/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["scheduler"] is None
    assert data["latest_report_week"] is None


def test_status_reports_latest_report_week_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)
    out_dir = tmp_path / "selfheal"
    consolidate_week("2026-W01", digests_dir=out_dir, out_dir=out_dir)

    resp = client.get("/api/selfheal/status")

    assert resp.status_code == 200
    assert resp.json()["latest_report_week"] == "2026-W01"


def test_status_enabled_with_scheduler_merges_status_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When self-heal is enabled and app.state carries a scheduler, its
    .status() dict is served verbatim under the `scheduler` key."""

    class _FakeScheduler:
        def status(self) -> dict[str, bool | str | None]:
            return {
                "alive": True,
                "last_daily_date": "2026-08-17",
                "last_weekly_week": "2026-W33",
                "next_expected_daily": "2026-08-18",
                "next_expected_weekly": "2026-W34",
            }

    app = create_app()
    app.state.selfheal_scheduler = _FakeScheduler()
    monkeypatch.setattr(
        selfheal_routes, "get_settings", lambda: Settings(use_mocks=True, self_heal_enabled=True)
    )
    local_client = TestClient(app)

    resp = local_client.get("/api/selfheal/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["scheduler"]["alive"] is True
    assert data["scheduler"]["last_weekly_week"] == "2026-W33"


def test_status_survives_a_corrupt_weekly_report_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted weekly/{week}.json must not break /status: latest_report_week
    degrades to null instead of the route erroring."""
    _redirect_root(monkeypatch, tmp_path)
    weekly_dir = tmp_path / "selfheal" / "weekly"
    weekly_dir.mkdir(parents=True)
    (weekly_dir / "2026-W34.json").write_text("{not valid json", encoding="utf-8")

    resp = client.get("/api/selfheal/status")

    assert resp.status_code == 200
    assert resp.json()["latest_report_week"] is None


# ── GET /api/selfheal/report ────────────────────────────────────────────────


def test_report_404_before_first_consolidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "no weekly report yet"


def test_report_200_after_report_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_root(monkeypatch, tmp_path)
    out_dir = tmp_path / "selfheal"
    digests_dir = out_dir
    selflogs_dir = tmp_path / "selflogs"
    day = "2026-08-17"  # Monday of 2026-W34
    _write_selflog(
        selflogs_dir, day, [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)]
    )
    scan_day(day, selflogs_dir=selflogs_dir, runs_dir=tmp_path / "runs", out_dir=digests_dir)
    written = consolidate_week("2026-W34", digests_dir=digests_dir, out_dir=out_dir)
    assert written.to_fix  # sanity: the fixture actually produced a fix candidate

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 200
    data = resp.json()
    assert data["week"] == "2026-W34"
    assert len(data["to_fix"]) == len(written.to_fix)
    assert data["to_fix"][0]["outcome"] is None  # no fix wave ran for this week


def test_report_merges_fix_outcome_onto_matching_to_fix_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the scheduler's weekly hook has persisted `{week}-outcomes.json`,
    the report route merges each outcome onto its matching `to_fix` cluster by
    fingerprint; report_only clusters never get an outcome."""
    _redirect_root(monkeypatch, tmp_path)
    out_dir = tmp_path / "selfheal"
    selflogs_dir = tmp_path / "selflogs"
    day = "2026-08-17"
    _write_selflog(
        selflogs_dir, day, [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)]
    )
    scan_day(day, selflogs_dir=selflogs_dir, runs_dir=tmp_path / "runs", out_dir=out_dir)
    report = consolidate_week("2026-W34", digests_dir=out_dir, out_dir=out_dir)
    assert report.to_fix

    fingerprint = report.to_fix[0].fingerprint
    outcome = FixOutcome(
        fingerprint=fingerprint,
        title=report.to_fix[0].title,
        run_id="deadbeef0001",
        status="pr_created",
        pr_url="https://github.com/kolz001/tvastr-demo/pull/1",
        pr_urls=("https://github.com/kolz001/tvastr-demo/pull/1",),
    )
    write_outcomes("2026-W34", [outcome], out_dir)

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 200
    data = resp.json()
    merged = next(c for c in data["to_fix"] if c["fingerprint"] == fingerprint)
    assert merged["outcome"]["status"] == "pr_created"
    assert merged["outcome"]["pr_url"] == "https://github.com/kolz001/tvastr-demo/pull/1"
    assert merged["outcome"]["run_id"] == "deadbeef0001"
    for cluster in data["report_only"]:
        assert cluster["outcome"] is None


def test_report_outcome_file_present_but_no_match_leaves_outcome_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outcomes file that exists but has no row for a given cluster's
    fingerprint (e.g. that cluster's run couldn't even be started) must leave
    outcome=None rather than raising or misattaching."""
    _redirect_root(monkeypatch, tmp_path)
    out_dir = tmp_path / "selfheal"
    selflogs_dir = tmp_path / "selflogs"
    day = "2026-08-17"
    _write_selflog(
        selflogs_dir, day, [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)]
    )
    scan_day(day, selflogs_dir=selflogs_dir, runs_dir=tmp_path / "runs", out_dir=out_dir)
    report = consolidate_week("2026-W34", digests_dir=out_dir, out_dir=out_dir)
    assert report.to_fix
    write_outcomes("2026-W34", [], out_dir)  # wave ran but produced nothing for this cluster

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 200
    assert resp.json()["to_fix"][0]["outcome"] is None


def test_report_corrupt_weekly_json_is_explained_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted weekly/{week}.json must not crash the route with a bare
    500 -- it should map to an HTTPException 500 with an explained detail
    (same pattern as the GitHub-502 mapping in api/routes/issues.py)."""
    _redirect_root(monkeypatch, tmp_path)
    weekly_dir = tmp_path / "selfheal" / "weekly"
    weekly_dir.mkdir(parents=True)
    (weekly_dir / "2026-W34.json").write_text("{not valid json", encoding="utf-8")

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 500
    assert "unreadable" in resp.json()["detail"]


def test_report_outcomes_file_with_missing_keys_leaves_outcome_null_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid-JSON outcomes file whose rows are missing required keys must
    not 500 -- load_outcomes degrades to None (task 3) and the report still
    returns 200 with outcome=null."""
    _redirect_root(monkeypatch, tmp_path)
    out_dir = tmp_path / "selfheal"
    selflogs_dir = tmp_path / "selflogs"
    day = "2026-08-17"
    _write_selflog(
        selflogs_dir, day, [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)]
    )
    scan_day(day, selflogs_dir=selflogs_dir, runs_dir=tmp_path / "runs", out_dir=out_dir)
    report = consolidate_week("2026-W34", digests_dir=out_dir, out_dir=out_dir)
    assert report.to_fix
    (out_dir / "weekly" / "2026-W34-outcomes.json").write_text(
        json.dumps([{"title": "x"}]), encoding="utf-8"
    )

    resp = client.get("/api/selfheal/report")

    assert resp.status_code == 200
    assert resp.json()["to_fix"][0]["outcome"] is None


# ── POST /api/selfheal/scan ─────────────────────────────────────────────────


def test_scan_daily_with_explicit_day_produces_digest_and_returns_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)
    day = "2026-08-18"
    _write_selflog(
        tmp_path / "selflogs",
        day,
        [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)],
    )

    resp = client.post("/api/selfheal/scan", json={"kind": "daily", "day": day})

    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "daily"
    assert data["day"] == day
    assert data["cluster_count"] >= 1
    assert data["event_count"] == 3
    assert (tmp_path / "selfheal" / "daily" / f"{day}.jsonl").exists()


def test_scan_daily_defaults_to_yesterday_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)
    expected_day = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

    resp = client.post("/api/selfheal/scan", json={"kind": "daily"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["day"] == expected_day
    # Proof-of-life write even on an empty day (scan_day's own convention).
    assert (tmp_path / "selfheal" / "daily" / f"{expected_day}.jsonl").exists()


def test_scan_daily_invalid_day_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_root(monkeypatch, tmp_path)

    resp = client.post("/api/selfheal/scan", json={"kind": "daily", "day": "not-a-date"})

    assert resp.status_code == 400


def test_scan_daily_out_of_range_calendar_day_is_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"2026-02-31" is shape-valid (YYYY-MM-DD) but not a real calendar date --
    date.fromisoformat raises ValueError deep inside the route. Must map to a
    clean 422, never a bare 500."""
    _redirect_root(monkeypatch, tmp_path)

    resp = client.post("/api/selfheal/scan", json={"kind": "daily", "day": "2026-02-31"})

    assert resp.status_code == 422
    assert "2026-02-31" in resp.json()["detail"]


def test_scan_weekly_with_explicit_week_produces_report_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)
    day = "2026-08-17"  # Monday of 2026-W34
    _write_selflog(
        tmp_path / "selflogs",
        day,
        [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)],
    )
    scan_day(
        day,
        selflogs_dir=tmp_path / "selflogs",
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "selfheal",
    )

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly", "week": "2026-W34"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "weekly"
    assert data["week"] == "2026-W34"
    assert "to_fix" in data and "report_only" in data
    assert (tmp_path / "selfheal" / "weekly" / "2026-W34.json").exists()


def test_scan_weekly_defaults_to_completed_previous_iso_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_root(monkeypatch, tmp_path)
    expected_week = week_key((datetime.now(UTC).date() - timedelta(days=7)).isoformat())

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly"})

    assert resp.status_code == 200
    assert resp.json()["week"] == expected_week


def test_scan_weekly_invalid_week_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_root(monkeypatch, tmp_path)

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly", "week": "bogus"})

    assert resp.status_code == 400


def test_scan_weekly_out_of_range_week_is_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"2026-W99" passes the YYYY-Www shape regex but ISO 2026 tops out at
    week 53 -- date.fromisocalendar raises ValueError deep inside the route.
    Must map to a clean 422, never a bare 500."""
    _redirect_root(monkeypatch, tmp_path)

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly", "week": "2026-W99"})

    assert resp.status_code == 422
    assert "2026-W99" in resp.json()["detail"]


def test_scan_manual_trigger_allowed_regardless_of_self_heal_enabled_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TVASTR_SELF_HEAL_ENABLED=false is sealed for the whole test process
    (conftest.py) -- every scan test above already proves this, but this test
    makes the guarantee explicit: the manual scan route is the demo path and
    must work even though the flag (and thus the background scheduler) is
    off."""
    _redirect_root(monkeypatch, tmp_path)
    settings = selfheal_routes.get_settings()
    assert settings.self_heal_enabled is False

    resp = client.post("/api/selfheal/scan", json={"kind": "daily", "day": "2026-08-18"})

    assert resp.status_code == 200


def test_scan_weekly_never_invokes_the_fix_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual scan must never open a PR: the route only ever calls
    scan_day/consolidate_week, never remediate.run_fix_wave. Patch run_fix_wave
    to blow up loudly if it's ever reached."""
    _redirect_root(monkeypatch, tmp_path)

    def _boom(*args: object, **kwargs: object) -> list[FixOutcome]:
        raise AssertionError("manual scan must never run the fix wave")

    monkeypatch.setattr(remediate_mod, "run_fix_wave", _boom)
    day = "2026-08-17"
    _write_selflog(
        tmp_path / "selflogs",
        day,
        [_error_record("run.failed: RuntimeError: boom", day) for _ in range(3)],
    )
    scan_day(
        day,
        selflogs_dir=tmp_path / "selflogs",
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "selfheal",
    )

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly", "week": "2026-W34"})

    assert resp.status_code == 200
    assert json.loads(resp.text)["to_fix"]  # the report still has fix candidates, just untouched


def test_scan_weekly_corrupt_daily_digest_is_explained_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted daily/{day}.jsonl must not crash the weekly consolidation
    route with a bare 500 -- it should map to an HTTPException 500 with an
    explained detail."""
    _redirect_root(monkeypatch, tmp_path)
    daily_dir = tmp_path / "selfheal" / "daily"
    daily_dir.mkdir(parents=True)
    day = "2026-08-17"  # Monday of 2026-W34
    (daily_dir / f"{day}.jsonl").write_text("{not valid json\n", encoding="utf-8")

    resp = client.post("/api/selfheal/scan", json={"kind": "weekly", "week": "2026-W34"})

    assert resp.status_code == 500
    assert "unreadable" in resp.json()["detail"]
