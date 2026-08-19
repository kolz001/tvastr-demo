"""Tests for the self-heal weekly consolidation + ranking module (report.py).

Digest fixtures are built through ``scan.py``'s own writer (``scan_day`` fed
synthetic selflog/run files under ``tmp_path``) so the on-disk cluster shape
can't drift out from under this module's assumptions — never hand-rolled
JSONL. Week 2026-W34 runs Monday 2026-08-17 through Sunday 2026-08-23.
"""

from __future__ import annotations

import json
from pathlib import Path

from tvastr.selfheal.report import (
    SEVERITY_WEIGHTS,
    RankedCluster,
    WeeklyReport,
    consolidate_week,
    load_latest_report,
    week_key,
)
from tvastr.selfheal.scan import scan_day

WEEK = "2026-W34"
MON, TUE, WED = "2026-08-17", "2026-08-18", "2026-08-19"


def _write_selflog(selflogs_dir: Path, day: str, records: list[dict]) -> None:
    from tvastr.selfheal.selflog import selflog_path

    path = selflog_path(selflogs_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _run_event(
    type_: str,
    step: str,
    payload: dict | None = None,
    *,
    day: str,
    ts: str | None = None,
    run_id: str = "run1",
) -> dict:
    return {
        "type": type_,
        "layer": "agent",
        "step": step,
        "payload": payload or {},
        "timestamp": ts or f"{day}T10:00:00.000000+00:00",
        "run_id": run_id,
    }


def _write_run(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _pipeline_start(run_id: str, day: str) -> dict:
    return _run_event(
        "pipeline.start",
        "pipeline",
        {"run_id": run_id, "repo": "run-llama/llama_index", "issue_number": 8001},
        day=day,
        ts=f"{day}T09:00:00.000000+00:00",
        run_id=run_id,
    )


def _pipeline_end(run_id: str, day: str) -> dict:
    return _run_event(
        "pipeline.end",
        "pipeline",
        {"outcome": "completed"},
        day=day,
        ts=f"{day}T11:00:00.000000+00:00",
        run_id=run_id,
    )


def _write_disk_full_day(selflogs_dir: Path, day: str, count: int) -> None:
    """One recurring selflog cluster ("disk full") with ``count`` occurrences."""
    _write_selflog(
        selflogs_dir,
        day,
        [
            {"event": "disk full", "level": "error", "timestamp": f"{day}T00:0{i}:00Z"}
            for i in range(count)
        ],
    )


# ── week_key ──────────────────────────────────────────────────────────────


def test_week_key_iso_week() -> None:
    assert week_key("2026-08-19") == "2026-W34"


# ── merging across days ──────────────────────────────────────────────────


def test_same_fingerprint_across_three_days_merges_with_summed_count(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    _write_disk_full_day(selflogs_dir, MON, 2)
    _write_disk_full_day(selflogs_dir, TUE, 3)
    _write_disk_full_day(selflogs_dir, WED, 1)
    for day in (MON, TUE, WED):
        scan_day(day, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    all_ranked = report.to_fix + report.report_only
    assert len(all_ranked) == 1
    cluster = all_ranked[0]
    assert cluster.count == 6
    assert cluster.title.startswith("disk full") or "disk full" in cluster.sample_messages[0]


# ── scoring: quality outranks higher-count ops ───────────────────────────


def test_quality_cluster_outranks_higher_count_ops_cluster(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    # Quality cluster: 2 confidence-0.0 investigate events in one run -> count 2.
    _write_run(
        runs_dir,
        "run-quality",
        [
            _pipeline_start("run-quality", MON),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "a"},
                day=MON,
                run_id="run-quality",
            ),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "b"},
                day=MON,
                run_id="run-quality",
            ),
            _pipeline_end("run-quality", MON),
        ],
    )
    # Ops cluster: 5 identical error events -> count 5.
    _write_run(
        runs_dir,
        "run-ops",
        [
            _pipeline_start("run-ops", MON),
            *[
                _run_event(
                    "error",
                    "issue_to_events",
                    {"reason": "no signature"},
                    day=MON,
                    run_id="run-ops",
                )
                for _ in range(5)
            ],
            _pipeline_end("run-ops", MON),
        ],
    )
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    ranked = report.to_fix + report.report_only
    by_kind = {c.kind: c for c in ranked}
    assert by_kind["quality"].count == 2
    assert by_kind["ops"].count == 5
    assert by_kind["quality"].score == 2 * SEVERITY_WEIGHTS["quality"]
    assert by_kind["ops"].score == 5 * SEVERITY_WEIGHTS["ops"]
    assert by_kind["quality"].score > by_kind["ops"].score
    # sorted desc by score -> quality ranks ahead of ops
    assert ranked.index(by_kind["quality"]) < ranked.index(by_kind["ops"])


# ── scoring: mapped-502 dampened below both ──────────────────────────────


def test_mapped_502_cluster_dampened_below_quality_and_ops(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    _write_run(
        runs_dir,
        "run-quality",
        [
            _pipeline_start("run-quality", MON),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "a"},
                day=MON,
                run_id="run-quality",
            ),
            _pipeline_end("run-quality", MON),
        ],
    )
    _write_run(
        runs_dir,
        "run-ops",
        [
            _pipeline_start("run-ops", MON),
            _run_event(
                "error", "issue_to_events", {"reason": "no signature"}, day=MON, run_id="run-ops"
            ),
            _pipeline_end("run-ops", MON),
        ],
    )
    # 502-mapped GitHub failure, repeated 3x: even at 3x the raw frequency of
    # the count-1 quality/ops clusters above, handled_upstream=0.25 sinks its
    # score (0.75) below both ops (1.0) and quality (3.0).
    _write_run(
        runs_dir,
        "run-github",
        [
            _pipeline_start("run-github", MON),
            *[
                _run_event(
                    "error",
                    "fetch_issue",
                    {
                        "reason": (
                            "GitHub issue search failed (403) — likely a transient "
                            "rate limit; retry shortly"
                        )
                    },
                    day=MON,
                    run_id="run-github",
                )
                for _ in range(3)
            ],
            _pipeline_end("run-github", MON),
        ],
    )
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    ranked = report.to_fix + report.report_only
    github_cluster = next(c for c in ranked if c.count == 3)
    ops_cluster = next(c for c in ranked if c.kind == "ops" and c.count != 3)
    quality_cluster = next(c for c in ranked if c.kind == "quality")
    assert github_cluster.severity_weight == SEVERITY_WEIGHTS["handled_upstream"]
    assert github_cluster.score < quality_cluster.score
    assert github_cluster.score < ops_cluster.score
    assert ranked[-1].fingerprint == github_cluster.fingerprint


# ── top_n truncation and fix_n split ──────────────────────────────────────


def test_top_n_truncation_and_fix_n_split(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    # 12 distinct ops clusters with descending counts 12..1 (score == count).
    # Purely-alphabetic step names -- clustering's fingerprint normalizer
    # collapses mixed alnum tokens like "step1"/"step12" to the same "<id>"
    # placeholder, which would wrongly merge these; alpha-only words survive.
    steps = [
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliett",
        "kilo",
        "lima",
    ]
    events = [_pipeline_start("run-many", MON)]
    for i, step in zip(range(12, 0, -1), steps, strict=True):
        events.extend(
            _run_event(
                "error",
                step,
                {"reason": "boom"},
                day=MON,
                run_id="run-many",
            )
            for _ in range(i)
        )
    events.append(_pipeline_end("run-many", MON))
    _write_run(runs_dir, "run-many", events)
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir, top_n=10, fix_n=3)

    assert len(report.to_fix) == 3
    assert len(report.report_only) == 7
    assert len(report.to_fix) + len(report.report_only) == 10
    counts = [c.count for c in report.to_fix + report.report_only]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 12


# ── empty week ─────────────────────────────────────────────────────────────


def test_empty_week_yields_empty_report_still_written(tmp_path: Path) -> None:
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    report = consolidate_week("2026-W01", digests_dir=digests_dir, out_dir=out_dir)

    assert report.to_fix == []
    assert report.report_only == []
    assert report.days_scanned == []
    assert report.week == "2026-W01"

    written = out_dir / "weekly" / "2026-W01.json"
    assert written.exists()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["week"] == "2026-W01"
    assert payload["to_fix"] == []
    assert payload["report_only"] == []


# ── load_latest_report ──────────────────────────────────────────────────


def test_load_latest_report_picks_lexically_latest_week(tmp_path: Path) -> None:
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    consolidate_week("2026-W01", digests_dir=digests_dir, out_dir=out_dir)
    consolidate_week("2026-W12", digests_dir=digests_dir, out_dir=out_dir)
    consolidate_week("2026-W02", digests_dir=digests_dir, out_dir=out_dir)

    latest = load_latest_report(out_dir)

    assert latest is not None
    assert isinstance(latest, WeeklyReport)
    assert latest.week == "2026-W12"


def test_load_latest_report_missing_dir_returns_none(tmp_path: Path) -> None:
    assert load_latest_report(tmp_path / "nope") is None


def test_load_latest_report_roundtrips_ranked_cluster_dataclass(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    _write_disk_full_day(selflogs_dir, MON, 3)
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)
    written = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    loaded = load_latest_report(out_dir)

    assert loaded is not None
    assert loaded.week == written.week
    assert loaded.days_scanned == written.days_scanned
    all_written = written.to_fix + written.report_only
    all_loaded = loaded.to_fix + loaded.report_only
    assert len(all_loaded) == len(all_written) == 1
    assert isinstance(all_loaded[0], RankedCluster)
    assert all_loaded[0].fingerprint == all_written[0].fingerprint
    assert all_loaded[0].count == 3


# ── days_scanned only reflects days a digest actually exists for ─────────


def test_days_scanned_reflects_only_present_daily_digests(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    _write_disk_full_day(selflogs_dir, MON, 1)
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)
    # WED intentionally never scanned -- missing days are fine.

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    assert report.days_scanned == [MON]


def _write_no_signature_run(runs_dir: Path, run_id: str, day: str) -> None:
    """A run refused for lack of a failure signature -- by-design behavior."""
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id, day),
            _run_event(
                "error",
                "issue_to_events",
                {"reason": "no error signature found in issue title or body"},
                day=day,
                run_id=run_id,
            ),
        ],
    )


def test_by_design_cluster_never_enters_to_fix(tmp_path: Path) -> None:
    """Regression pinned from the first real weekly report (2026-W27).

    234 'no error signature found' refusals (working-as-intended) outscored a
    genuine confidence-0.0 quality signal 234.0 to 6.0. By-design clusters
    must be dampened AND barred from to_fix regardless of rank.
    """
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    for i in range(5):  # recurring by-design refusals (distilled from 234)
        _write_no_signature_run(runs_dir, f"run-nosig-{i}", MON)
    _write_run(
        runs_dir,
        "run-quality",
        [
            _pipeline_start("run-quality", MON),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "stuck"},
                day=MON,
                run_id="run-quality",
            ),
            _pipeline_end("run-quality", MON),
        ],
    )
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    fix_titles = [c.title for c in report.to_fix]
    assert all("no error signature" not in t for t in fix_titles)
    assert any("confidence 0.0" in t for t in fix_titles)
    nosig = [c for c in report.report_only if "no error signature" in c.title]
    assert len(nosig) == 1
    assert nosig[0].severity_weight == SEVERITY_WEIGHTS["by_design"]
    assert nosig[0].kind == "ops"  # kind stays honest; only weight/placement change


def test_by_design_excluded_from_to_fix_even_with_room(tmp_path: Path) -> None:
    """A by-design cluster is report-only even when to_fix has empty slots."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    digests_dir = tmp_path / "digests"
    out_dir = tmp_path / "weekly_out"

    _write_no_signature_run(runs_dir, "run-nosig", MON)
    scan_day(MON, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=digests_dir)

    report = consolidate_week(WEEK, digests_dir=digests_dir, out_dir=out_dir)

    assert report.to_fix == []
    assert len(report.report_only) == 1
