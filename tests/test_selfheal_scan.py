"""Tests for the self-heal daily digest scanner (scan.py).

Builds synthetic selflog + run files under tmp_path — never touches the real
data/runs on this machine. The loop-guard test (test_self_run_contributes_zero_
candidates) is the hard requirement: a self-heal-launched run must never feed
its own ordinary events back into the digest, or the loop could chase its own
tail forever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tvastr.domain import Severity
from tvastr.selfheal.scan import DailyDigest, collect_candidates, load_daily, scan_day
from tvastr.selfheal.selflog import selflog_path

DAY = "2026-08-19"


def _write_selflog(selflogs_dir: Path, day: str, records: list[dict]) -> None:
    path = selflog_path(selflogs_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _run_event(
    type_: str,
    step: str,
    payload: dict | None = None,
    *,
    ts: str = f"{DAY}T10:00:00.000000+00:00",
    run_id: str = "run1",
) -> dict:
    return {
        "type": type_,
        "layer": "agent",
        "step": step,
        "payload": payload or {},
        "timestamp": ts,
        "run_id": run_id,
    }


def _write_run(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _pipeline_start(run_id: str, *, self_heal: bool = False, ts: str | None = None) -> dict:
    payload = {
        "run_id": run_id,
        "repo": "run-llama/llama_index",
        "issue_number": 8001,
        "issue_title": "some issue",
    }
    if self_heal:
        payload["self_heal"] = True
    return _run_event(
        "pipeline.start",
        "pipeline",
        payload,
        ts=ts or f"{DAY}T09:00:00.000000+00:00",
        run_id=run_id,
    )


def _pipeline_end(run_id: str, ts: str | None = None) -> dict:
    return _run_event(
        "pipeline.end",
        "pipeline",
        {"outcome": "completed"},
        ts=ts or f"{DAY}T11:00:00.000000+00:00",
        run_id=run_id,
    )


# ── (a) selflog error/critical records surface; info doesn't ────────────────


def test_error_selflog_records_surface_info_does_not(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {
                "event": "boom",
                "level": "error",
                "timestamp": f"{DAY}T00:00:00Z",
                "exception": "Trace",
            },
            {"event": "all fine", "level": "info", "timestamp": f"{DAY}T00:00:01Z"},
            {"event": "critical thing", "level": "critical", "timestamp": f"{DAY}T00:00:02Z"},
        ],
    )

    events, scanned_runs, skipped_self_runs = collect_candidates(DAY, selflogs_dir, runs_dir)

    messages = {e.message for e in events}
    assert messages == {"boom", "critical thing"}
    assert scanned_runs == 0
    assert skipped_self_runs == 0
    boom = next(e for e in events if e.message == "boom")
    assert boom.source == "selfheal"
    assert boom.severity == Severity.ERROR
    assert boom.stack_trace == "Trace"
    assert boom.attributes["origin"] == "selflog"


def test_selflog_self_heal_records_skipped(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {
                "event": "own loop error",
                "level": "error",
                "timestamp": f"{DAY}T00:00:00Z",
                "self_heal": True,
            },
        ],
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert events == []


# ── (b) run file with 2 error events yields ops candidates carrying run_id ──


def test_run_error_events_yield_ops_candidates_with_run_id(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-with-errors"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event("error", "issue_to_events", {"reason": "no signature"}, run_id=run_id),
            _run_event("error", "verify_setup", {"reason": "sandbox unavailable"}, run_id=run_id),
            _pipeline_end(run_id),
        ],
    )

    events, scanned_runs, skipped_self_runs = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert scanned_runs == 1
    assert skipped_self_runs == 0
    assert len(events) == 2
    for event in events:
        assert event.attributes["run_id"] == run_id
        assert event.attributes["origin"] == "runs"
    reasons = {e.message for e in events}
    assert reasons == {
        "issue_to_events: no signature",
        "verify_setup: sandbox unavailable",
    }


# ── (c) confidence-0.0 run yields exactly one quality candidate ─────────────


def test_confidence_zero_yields_exactly_one_quality_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-zero-confidence"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "insufficient evidence"},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    events, scanned_runs, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert scanned_runs == 1
    assert len(events) == 1
    candidate = events[0]
    assert candidate.message == "investigator returned confidence 0.0"
    assert candidate.attributes["run_id"] == run_id
    assert candidate.attributes["issue"] == "run-llama/llama_index#8001"


def test_nonzero_confidence_yields_no_quality_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-good-confidence"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.8, "summary": "clear cause"},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert events == []


def test_two_investigate_events_one_zero_one_nonzero_yields_one_candidate(
    tmp_path: Path,
) -> None:
    """A run file with two investigate events (agent runs once per detected
    pattern) — one confidence 0.0, one 0.8 — must yield exactly one
    confidence-zero candidate, for the 0.0 event only."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-mixed-confidence"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "pattern A insufficient evidence"},
                run_id=run_id,
            ),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.8, "summary": "pattern B clear cause"},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    events, scanned_runs, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert scanned_runs == 1
    assert len(events) == 1
    assert events[0].message == "investigator returned confidence 0.0"


def test_two_zero_confidence_investigate_events_yield_two_candidates(
    tmp_path: Path,
) -> None:
    """A run file with two confidence-0.0 investigate events (two detected
    patterns, both under-confident) must yield two quality candidates."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-double-zero-confidence"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "pattern A insufficient evidence"},
                run_id=run_id,
            ),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "pattern B insufficient evidence"},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    events, scanned_runs, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert scanned_runs == 1
    assert len(events) == 2
    assert all(e.message == "investigator returned confidence 0.0" for e in events)


# ── (d) loop guard: self_heal run contributes zero candidates ───────────────


def test_self_run_contributes_zero_candidates(tmp_path: Path) -> None:
    """The hard requirement. An otherwise-identical run tagged self_heal:true in
    pipeline.start's run_meta must contribute nothing to the digest — not the
    error events, not the confidence-0.0 signal — and must be counted in
    skipped_self_runs, not scanned_runs."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "self-heal-run"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id, self_heal=True),
            _run_event("error", "issue_to_events", {"reason": "no signature"}, run_id=run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "n/a"},
                run_id=run_id,
            ),
            _run_event("verify.result", "verify", {"verdict": "repro_broken"}, run_id=run_id),
            _pipeline_end(run_id),
        ],
    )

    events, scanned_runs, skipped_self_runs = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert events == []
    assert scanned_runs == 0
    assert skipped_self_runs == 1


def test_self_run_alongside_normal_run_only_normal_run_counted(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    _write_run(
        runs_dir,
        "self-run",
        [
            _pipeline_start("self-run", self_heal=True),
            _run_event("error", "step", {"reason": "x"}, run_id="self-run"),
            _pipeline_end("self-run"),
        ],
    )
    _write_run(
        runs_dir,
        "normal-run",
        [
            _pipeline_start("normal-run"),
            _run_event("error", "step", {"reason": "y"}, run_id="normal-run"),
            _pipeline_end("normal-run"),
        ],
    )

    events, scanned_runs, skipped_self_runs = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert scanned_runs == 1
    assert skipped_self_runs == 1
    assert len(events) == 1
    assert events[0].attributes["run_id"] == "normal-run"


# ── verify REPRO_BROKEN and llm.call failure signals ─────────────────────────


def test_repro_broken_verdict_yields_one_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-repro-broken"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _pipeline_end(run_id),
            # verify.* events append AFTER pipeline.end — must still be scanned.
            _run_event(
                "verify.result",
                "verify",
                {"verdict": "repro_broken", "rerun_stderr_tail": "Traceback..."},
                ts=f"{DAY}T11:05:00.000000+00:00",
                run_id=run_id,
            ),
        ],
    )

    events, scanned_runs, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert scanned_runs == 1
    assert len(events) == 1
    assert events[0].message == "verify verdict repro_broken"
    assert events[0].stack_trace == "Traceback..."


def test_verified_verdict_yields_no_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-verified"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _pipeline_end(run_id),
            _run_event(
                "verify.result", "verify", {"verdict": "verified_via_reproducer"}, run_id=run_id
            ),
        ],
    )
    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert events == []


def test_llm_call_failure_yields_one_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-llm-failure"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "llm.call",
                "root_cause",
                {"task": "root_cause", "success": False, "error": "timeout"},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )
    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert len(events) == 1
    assert "timeout" in events[0].message


def test_successful_llm_call_yields_no_candidate(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-llm-ok"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "llm.call",
                "root_cause",
                {"task": "root_cause", "mocked": False, "elapsed_ms": 100},
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )
    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert events == []


# ── run-completeness: non-terminated runs are not mined ─────────────────────


def test_run_without_any_terminal_event_is_not_scanned(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-in-flight"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "agent.node.end",
                "investigate",
                {"confidence": 0.0, "summary": "n/a"},
                run_id=run_id,
            ),
            # no pipeline.end / pipeline.interrupted / error event — the run
            # never reached a terminal event, so it must not be mined yet.
        ],
    )
    events, scanned_runs, skipped_self_runs = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert events == []
    assert scanned_runs == 0
    assert skipped_self_runs == 0


# ── (e) recurring identical messages cluster into one FailurePattern ────────


def test_recurring_identical_messages_cluster_with_correct_count(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "digests"
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {
                "event": "connection refused to redis",
                "level": "error",
                "timestamp": f"{DAY}T00:00:00Z",
            },
            {
                "event": "connection refused to redis",
                "level": "error",
                "timestamp": f"{DAY}T00:01:00Z",
            },
            {
                "event": "connection refused to redis",
                "level": "error",
                "timestamp": f"{DAY}T00:02:00Z",
            },
        ],
    )

    digest = scan_day(DAY, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=out_dir)

    assert len(digest.clusters) == 1
    assert digest.clusters[0].count == 3
    assert digest.clusters[0].is_recurring


# ── (f) corrupt JSONL lines are skipped without raising ─────────────────────


def test_corrupt_selflog_lines_skipped_without_raising(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    path = selflog_path(selflogs_dir, DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{not valid json\n"
        + json.dumps({"event": "real error", "level": "error", "timestamp": f"{DAY}T00:00:00Z"})
        + "\n"
        + '"just a string"\n'
        + "\n",  # blank line
        encoding="utf-8",
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert [e.message for e in events] == ["real error"]


def test_corrupt_run_lines_skipped_without_raising(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    run_id = "run-corrupt"
    path = runs_dir / f"{run_id}.jsonl"
    path.write_text(
        json.dumps(_pipeline_start(run_id)) + "\n"
        "{not valid json\n"
        + json.dumps(_run_event("error", "step", {"reason": "z"}, run_id=run_id))
        + "\n"
        + json.dumps(_pipeline_end(run_id))
        + "\n",
        encoding="utf-8",
    )

    events, scanned_runs, _ = collect_candidates(DAY, selflogs_dir, runs_dir)
    assert scanned_runs == 1
    assert len(events) == 1
    assert events[0].message == "step: z"


# ── source timestamps: candidates must carry the real event time, not utcnow ─


def test_selflog_candidate_preserves_record_timestamp(tmp_path: Path) -> None:
    """A selflog LogEvent's timestamp must come from the record's own
    ``timestamp`` field, not construction time."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    old_ts = datetime.now(UTC) - timedelta(days=5)
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {
                "event": "old failure",
                "level": "error",
                "timestamp": old_ts.isoformat(),
            }
        ],
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert len(events) == 1
    assert events[0].timestamp == old_ts
    assert (datetime.now(UTC) - events[0].timestamp) > timedelta(days=1)


def test_selflog_candidate_falls_back_silently_on_missing_or_corrupt_timestamp(
    tmp_path: Path,
) -> None:
    """A missing/corrupt ``timestamp`` field must not raise — the LogEvent
    falls back to its default (construction-time) timestamp."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {"event": "no timestamp field", "level": "error"},
            {"event": "corrupt timestamp", "level": "error", "timestamp": "not-a-timestamp"},
        ],
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert len(events) == 2
    for event in events:
        assert (datetime.now(UTC) - event.timestamp) < timedelta(minutes=5)


def test_run_error_candidate_preserves_event_timestamp(tmp_path: Path) -> None:
    """A run-event LogEvent's timestamp must come from the source
    PipelineEvent's own timestamp, not construction time."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    run_id = "run-old-error"
    old_ts = datetime.now(UTC) - timedelta(days=5)
    old_ts_iso = old_ts.isoformat()
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "error",
                "issue_to_events",
                {"reason": "no signature"},
                ts=old_ts_iso,
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    events, _, _ = collect_candidates(DAY, selflogs_dir, runs_dir)

    assert len(events) == 1
    assert events[0].timestamp == old_ts
    assert (datetime.now(UTC) - events[0].timestamp) > timedelta(days=1)


def test_digest_cluster_first_last_seen_reflect_source_timestamps(tmp_path: Path) -> None:
    """The clustered FailurePattern's first_seen/last_seen must be derived
    from the candidates' real source timestamps, not from utcnow — construct
    a selflog record and a run error event with a known, fixed timestamp and
    assert the resulting cluster reflects it exactly rather than "now"."""
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "digests"
    known_ts = datetime.fromisoformat(f"{DAY}T03:15:00+00:00")
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {
                "event": "ancient failure",
                "level": "error",
                "timestamp": known_ts.isoformat(),
            }
        ],
    )
    run_id = "run-ancient-error"
    # events[0] (pipeline.start) must keep a DAY-prefixed timestamp so the
    # run file is selected for this day; the error event itself carries the
    # known timestamp under test.
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event(
                "error",
                "step",
                {"reason": "ancient"},
                ts=known_ts.isoformat(),
                run_id=run_id,
            ),
            _pipeline_end(run_id),
        ],
    )

    digest = scan_day(DAY, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=out_dir)

    assert len(digest.clusters) == 2
    for cluster in digest.clusters:
        assert cluster.first_seen == known_ts
        assert cluster.last_seen == known_ts
        assert (datetime.now(UTC) - cluster.first_seen) > timedelta(minutes=5)


# ── (g) roundtrip scan_day -> load_daily ─────────────────────────────────────


def test_roundtrip_scan_day_load_daily(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "digests"
    _write_selflog(
        selflogs_dir,
        DAY,
        [
            {"event": "disk full", "level": "error", "timestamp": f"{DAY}T00:00:00Z"},
            {"event": "disk full", "level": "error", "timestamp": f"{DAY}T00:01:00Z"},
        ],
    )
    run_id = "run-for-roundtrip"
    _write_run(
        runs_dir,
        run_id,
        [
            _pipeline_start(run_id),
            _run_event("error", "step", {"reason": "boom"}, run_id=run_id),
            _pipeline_end(run_id),
        ],
    )

    written = scan_day(DAY, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=out_dir)
    loaded = load_daily(out_dir, DAY)

    assert loaded is not None
    assert isinstance(loaded, DailyDigest)
    assert loaded.day == written.day == DAY
    assert loaded.scanned_runs == written.scanned_runs == 1
    assert loaded.skipped_self_runs == written.skipped_self_runs == 0
    assert len(loaded.clusters) == len(written.clusters) == 2
    assert {c.fingerprint for c in loaded.clusters} == {c.fingerprint for c in written.clusters}
    assert {c.count for c in loaded.clusters} == {c.count for c in written.clusters}
    # events are not persisted verbatim — only up to 3 sample messages/cluster.
    assert loaded.events == []
    assert len(written.events) == 3


def test_load_daily_missing_day_returns_none(tmp_path: Path) -> None:
    assert load_daily(tmp_path / "digests", "2099-01-01") is None


def test_empty_day_still_writes_header_only_file(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "digests"

    digest = scan_day(DAY, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=out_dir)

    assert digest.clusters == []
    assert digest.events == []
    assert digest.scanned_runs == 0
    assert digest.skipped_self_runs == 0

    path = out_dir / "daily" / f"{DAY}.jsonl"
    assert path.exists()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    header = json.loads(lines[0])
    assert header == {
        "day": DAY,
        "cluster_count": 0,
        "event_count": 0,
        "scanned_runs": 0,
        "skipped_self_runs": 0,
    }

    loaded = load_daily(out_dir, DAY)
    assert loaded is not None
    assert loaded.clusters == []


def test_digest_lines_carry_sample_messages(tmp_path: Path) -> None:
    selflogs_dir = tmp_path / "selflogs"
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "digests"
    _write_selflog(
        selflogs_dir,
        DAY,
        [{"event": "timeout waiting for redis", "level": "error", "timestamp": f"{DAY}T00:00:00Z"}],
    )

    scan_day(DAY, selflogs_dir=selflogs_dir, runs_dir=runs_dir, out_dir=out_dir)

    path = out_dir / "daily" / f"{DAY}.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    cluster_row = json.loads(lines[1])
    assert cluster_row["sample_messages"] == ["timeout waiting for redis"]
    assert cluster_row["count"] == 1
