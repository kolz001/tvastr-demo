"""Tests for the self-remediation fix wave + Slack escalation (selfheal/remediate.py).

Two flavours of test live here:

* **Injected ``start_run``** — for everything about *what the wave asks for*
  (how many runs, what settings/run_meta they carry) and *how it reads outcomes
  back* (canned run files written through the sink the wave handed over).
* **The real pipeline in mock mode** — for the properties an injected starter
  cannot prove: that the default starter actually runs and *joins* each
  pipeline thread before the next one starts, and that a run file the wave
  produced is skipped by Task 2's ``scan_day`` loop guard (the end-to-end proof
  that the self-healing loop closes instead of feeding on itself).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

from tvastr.config import Settings
from tvastr.domain import LogEvent
from tvastr.events import EventSink, JsonlEventSink, PipelineEvent, run_path
from tvastr.integrations.slack import MockSlackNotifier
from tvastr.selfheal import remediate as remediate_mod
from tvastr.selfheal.remediate import (
    FixOutcome,
    _default_start_run,
    build_escalation,
    escalate,
    run_fix_wave,
)
from tvastr.selfheal.report import RankedCluster, WeeklyReport
from tvastr.selfheal.scan import scan_day

SELF_REPO = "kolz001/tvastr-selftest"


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "use_mocks": True,
        "audit_backend": "memory",
        "recurrence_threshold": 3,
        "self_heal_repo": SELF_REPO,
        "github_repo": "run-llama/llama_index",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _cluster(
    fingerprint: str,
    title: str,
    *,
    count: int = 4,
    samples: list[str] | None = None,
    kind: str = "ops",
) -> RankedCluster:
    return RankedCluster(
        fingerprint=fingerprint,
        title=title,
        count=count,
        severity_weight=1.0,
        score=float(count),
        kind=kind,
        sample_messages=samples if samples is not None else [f"{title}: boom"],
        first_seen="2026-08-17T09:00:00+00:00",
        last_seen="2026-08-19T21:30:00+00:00",
    )


def _report(
    to_fix: list[RankedCluster], report_only: list[RankedCluster] | None = None
) -> WeeklyReport:
    return WeeklyReport(
        week="2026-W34",
        generated_at="2026-08-24T02:00:00+00:00",
        to_fix=to_fix,
        report_only=report_only or [],
        days_scanned=["2026-08-17", "2026-08-18"],
    )


def _event(type_: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": type_, "layer": "output", "step": "pipeline", "payload": payload or {}}


def _starter(
    calls: list[dict[str, Any]],
    canned: dict[str, list[dict[str, Any]]] | None = None,
    *,
    raises: str | None = None,
) -> Callable[..., str]:
    """An injectable ``start_run``: records its call and writes a canned run file.

    It writes through the *sink the wave handed it*, so a wrong sink path would
    make every outcome assertion below fail rather than silently pass.
    """

    def _start(
        *,
        events: list[LogEvent],
        run_meta: dict[str, Any],
        settings: Settings,
        sink: EventSink,
        run_id: str,
    ) -> str:
        calls.append(
            {
                "events": events,
                "run_meta": run_meta,
                "settings": settings,
                "sink": sink,
                "run_id": run_id,
            }
        )
        fingerprint = str(run_meta["fingerprint"])
        if raises is not None and fingerprint == raises:
            raise RuntimeError("starter exploded")
        default = [_event("pipeline.end", {"outcome": "skipped"})]
        for row in (canned or {}).get(fingerprint, default):
            sink.emit(
                PipelineEvent(
                    type=row["type"],
                    layer=row["layer"],
                    step=row["step"],
                    run_id=run_id,
                    payload=row["payload"],
                )
            )
        return run_id

    return _start


# ── the wave: what it starts ──────────────────────────────────────────────


def test_wave_starts_one_run_per_to_fix_cluster_with_self_heal_meta(tmp_path: Path) -> None:
    report = _report([_cluster("fp1", "AError"), _cluster("fp2", "BError")])
    calls: list[dict[str, Any]] = []

    outcomes = run_fix_wave(
        report, settings=_settings(), runs_dir=tmp_path, start_run=_starter(calls)
    )

    assert len(calls) == 2
    assert [c["run_meta"]["fingerprint"] for c in calls] == ["fp1", "fp2"]
    assert [o.fingerprint for o in outcomes] == ["fp1", "fp2"]
    for call in calls:
        meta = call["run_meta"]
        assert meta["self_heal"] is True
        assert meta["run_id"] == call["run_id"]
        assert meta["mode"] == "mock"
        assert meta["cluster_title"]
        # The self-run targets tvastr's OWN repo, never the testbed repo, and
        # bypasses the recurrence threshold (the cluster already recurred).
        assert call["settings"].github_repo == SELF_REPO
        assert call["settings"].recurrence_threshold == 1


def test_wave_reconstructs_representative_events_from_samples_and_metadata(
    tmp_path: Path,
) -> None:
    cluster = _cluster(
        "fp1",
        "TimeoutError",
        samples=["llm.call failed (fix_generation): timeout", "llm.call failed (judge): timeout"],
    )
    calls: list[dict[str, Any]] = []

    run_fix_wave(
        _report([cluster]), settings=_settings(), runs_dir=tmp_path, start_run=_starter(calls)
    )

    events: list[LogEvent] = calls[0]["events"]
    assert [e.message for e in events] == cluster.sample_messages
    assert {e.service for e in events} == {"tvastr-self"}
    assert {e.source for e in events} == {"selfheal"}
    assert events[0].timestamp == datetime.fromisoformat(cluster.first_seen)
    assert events[-1].timestamp == datetime.fromisoformat(cluster.last_seen)
    assert events[0].attributes["fingerprint"] == "fp1"


def test_wave_falls_back_to_the_cluster_title_when_no_samples_survived(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    run_fix_wave(
        _report([_cluster("fp1", "OpaqueFailure", samples=[])]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=_starter(calls),
    )

    assert [e.message for e in calls[0]["events"]] == ["OpaqueFailure"]


def test_wave_forces_dry_run_by_default(tmp_path: Path) -> None:
    """FINDING 4: without explicit opt-in, the wave must never open real PRs —
    ``self_heal_open_prs`` defaults False, so the run settings must be forced
    into dry_run regardless of the caller's own dry_run value."""
    calls: list[dict[str, Any]] = []

    run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(dry_run=False),
        runs_dir=tmp_path,
        start_run=_starter(calls),
    )

    assert calls[0]["settings"].dry_run is True


def test_wave_respects_dry_run_false_when_open_prs_enabled(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(dry_run=False, self_heal_open_prs=True),
        runs_dir=tmp_path,
        start_run=_starter(calls),
    )

    assert calls[0]["settings"].dry_run is False


def test_wave_with_no_to_fix_clusters_starts_nothing(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    outcomes = run_fix_wave(
        _report([], [_cluster("fp9", "OnlyReported")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=_starter(calls),
    )

    assert calls == []
    assert outcomes == []


# ── the wave: how it reads outcomes back ──────────────────────────────────


def test_pr_opened_event_yields_pr_created_with_url(tmp_path: Path) -> None:
    url = f"https://github.com/{SELF_REPO}/pull/900001"
    calls: list[dict[str, Any]] = []
    starter = _starter(
        calls,
        {
            "fp1": [
                _event("pr.opened", {"url": url, "number": 900001}),
                _event("pipeline.end", {"outcome": "pr_opened"}),
            ]
        },
    )

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert outcomes[0].status == "pr_created"
    assert outcomes[0].pr_url == url
    assert outcomes[0].run_id == calls[0]["run_id"]


def test_audit_saved_pull_request_url_also_yields_pr_created(tmp_path: Path) -> None:
    url = f"https://github.com/{SELF_REPO}/pull/42"
    starter = _starter(
        [],
        {
            "fp1": [
                _event("audit.saved", {"outcome": "pr_opened", "pull_request_url": url}),
                _event("pipeline.end", {"outcome": "pr_opened"}),
            ]
        },
    )

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert (outcomes[0].status, outcomes[0].pr_url) == ("pr_created", url)


def test_terminal_run_without_a_pr_is_unverified(tmp_path: Path) -> None:
    starter = _starter(
        [],
        {
            "fp1": [
                _event("audit.saved", {"outcome": "skipped", "pull_request_url": None}),
                _event("pipeline.end", {"outcome": "skipped"}),
            ]
        },
    )

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert outcomes[0].status == "unverified"
    assert outcomes[0].pr_url is None


def test_error_terminal_run_is_failed(tmp_path: Path) -> None:
    starter = _starter([], {"fp1": [_event("error", {"error": "RuntimeError", "message": "boom"})]})

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert outcomes[0].status == "failed"


def test_run_file_with_no_terminal_event_is_failed(tmp_path: Path) -> None:
    starter = _starter([], {"fp1": [_event("ingest.read", {"count": 1})]})

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert outcomes[0].status == "failed"


def test_multiple_pr_opened_events_yield_all_urls_and_pr_url_is_the_first(
    tmp_path: Path,
) -> None:
    """FINDING 3: one cluster can open multiple PRs (one per detected pattern
    reconstructed from its sample messages), but the old extraction returned
    only the first — the rest were invisible to the escalation summary."""
    url1 = f"https://github.com/{SELF_REPO}/pull/101"
    url2 = f"https://github.com/{SELF_REPO}/pull/102"
    starter = _starter(
        [],
        {
            "fp1": [
                _event("pr.opened", {"url": url1, "number": 101}),
                _event("pr.opened", {"url": url2, "number": 102}),
                _event("pipeline.end", {"outcome": "pr_opened"}),
            ]
        },
    )

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert outcomes[0].status == "pr_created"
    assert outcomes[0].pr_url == url1  # interface frozen: pr_url stays the FIRST
    assert outcomes[0].pr_urls == (url1, url2)

    message = build_escalation(_report([_cluster("fp1", "AError")]), outcomes)
    assert url1 in message
    assert url2 in message


def test_starter_exception_is_failed_and_does_not_stop_the_wave(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    starter = _starter(calls, raises="fp1")

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError"), _cluster("fp2", "BError")]),
        settings=_settings(),
        runs_dir=tmp_path,
        start_run=starter,
    )

    assert [(o.fingerprint, o.status) for o in outcomes] == [
        ("fp1", "failed"),
        ("fp2", "unverified"),
    ]


def test_read_outcome_exception_does_not_lose_earlier_completed_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING 5: work outside the per-cluster try (here: ``_read_outcome``)
    must never lose an already-completed outcome. A real opened PR (cluster 1)
    must survive a catastrophic failure reading back cluster 2's run file, and
    cluster 3 must still be attempted."""
    calls: list[dict[str, Any]] = []
    url = f"https://github.com/{SELF_REPO}/pull/77"
    starter = _starter(
        calls,
        {
            "fp1": [
                _event("pr.opened", {"url": url, "number": 77}),
                _event("pipeline.end", {"outcome": "pr_opened"}),
            ]
        },
    )

    original_read_outcome = remediate_mod._read_outcome

    def _flaky_read_outcome(cluster: RankedCluster, run_id: str, runs_dir: Path) -> FixOutcome:
        if cluster.fingerprint == "fp2":
            raise RuntimeError("disk exploded reading run file")
        return original_read_outcome(cluster, run_id, runs_dir)

    monkeypatch.setattr(remediate_mod, "_read_outcome", _flaky_read_outcome)

    report = _report(
        [_cluster("fp1", "AError"), _cluster("fp2", "BError"), _cluster("fp3", "CError")]
    )
    outcomes = run_fix_wave(report, settings=_settings(), runs_dir=tmp_path, start_run=starter)

    assert [o.fingerprint for o in outcomes] == ["fp1", "fp2", "fp3"]
    assert outcomes[0].status == "pr_created"  # not lost/downgraded to skipped
    assert outcomes[0].pr_url == url
    assert outcomes[1].status == "failed"
    assert len(calls) == 3  # cluster3 was still attempted


def test_prologue_failure_returns_skipped_outcomes_for_all_clusters_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING 5: ``run_fix_wave`` itself must never raise. A catastrophic
    failure before any cluster is attempted (here: settings prep) must still
    return one outcome per to_fix cluster, reported skipped."""

    def _boom(settings: Settings) -> Settings:
        raise RuntimeError("settings prep exploded")

    monkeypatch.setattr(remediate_mod, "_self_run_settings", _boom)

    report = _report([_cluster("fp1", "AError"), _cluster("fp2", "BError")])
    outcomes = run_fix_wave(report, settings=_settings(), runs_dir=tmp_path)

    assert [(o.fingerprint, o.status) for o in outcomes] == [
        ("fp1", "skipped"),
        ("fp2", "skipped"),
    ]


# ── the wave: the real default starter (mock mode, offline) ───────────────


def test_default_starter_runs_the_real_pipeline_sequentially_and_joins(tmp_path: Path) -> None:
    """Every run must be finished (thread joined, terminal event on disk) before
    the next one starts — two self-fixes must never race on one repo."""
    from tvastr.runner import IN_FLIGHT

    report = _report(
        [
            _cluster("fp1", "ImportError", samples=["ImportError: cannot import name 'X'"]),
            _cluster("fp2", "TypeError", samples=["TypeError: unsupported operand type"]),
        ]
    )

    outcomes = run_fix_wave(report, settings=_settings(), runs_dir=tmp_path)

    assert len(outcomes) == 2
    live = [t for t in threading.enumerate() if t.name.startswith("tvastr-run-")]
    assert live == []
    for outcome in outcomes:
        assert outcome.run_id is not None
        assert outcome.run_id not in IN_FLIGHT
        rows = [
            json.loads(line)
            for line in run_path(outcome.run_id, tmp_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(r["type"] in {"pipeline.end", "pipeline.interrupted", "error"} for r in rows)
        start = next(r for r in rows if r["type"] == "pipeline.start")
        assert start["payload"]["self_heal"] is True
        assert start["payload"]["repo"] == SELF_REPO


def test_default_starter_opens_a_pr_against_the_self_heal_repo(tmp_path: Path) -> None:
    """Mock mode still exercises the whole agent; the mock code host mints a PR
    URL for whichever repo the run's settings named — proving the github_repo
    override reaches the code host, not just the run_meta. Requires explicit
    opt-in (FINDING 4: the wave is forced dry_run unless self_heal_open_prs is
    on, so real/mock PR creation no longer happens by default)."""
    report = _report([_cluster("fp1", "ImportError", samples=["ImportError: no module named x"])])

    outcomes = run_fix_wave(
        report, settings=_settings(self_heal_open_prs=True), runs_dir=tmp_path
    )

    assert outcomes[0].status == "pr_created"
    assert outcomes[0].pr_url is not None
    assert SELF_REPO in outcomes[0].pr_url


# ── selflog-axis loop hole: self-heal-thread logs must carry self_heal ────


def test_default_start_run_binds_self_heal_contextvar_for_the_thread_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any log emitted by application code running inside the self-heal
    thread body must carry ``self_heal=True`` via structlog contextvars (NOT
    just the terminal error event runner.py produces) -- otherwise a stray
    error logged mid-run (e.g. a warning from deep in the pipeline) lands in
    the selflog file unmarked and scan.py mines it as a fresh failure.
    Contextvars don't propagate into new threads, so the bind must happen
    INSIDE the thread body, not in the wave's loop."""
    captured: dict[str, Any] = {}

    class _FakeThreshold:
        recurrence_threshold: int | None = None

    class _FakePipeline:
        def __init__(self) -> None:
            self.threshold = _FakeThreshold()

        def run(self, **kwargs: Any) -> None:
            captured["ctx"] = dict(structlog.contextvars.get_contextvars())

    def _fake_build_pipeline(*args: Any, **kwargs: Any) -> _FakePipeline:
        return _FakePipeline()

    monkeypatch.setattr(remediate_mod, "build_pipeline", _fake_build_pipeline)

    path = tmp_path / "ctxrun.jsonl"
    _default_start_run(
        events=[],
        run_meta={"self_heal": True},
        settings=_settings(),
        sink=JsonlEventSink(path),
        run_id="ctxrun",
    )

    assert captured["ctx"].get("self_heal") is True
    # The main thread's own context must be untouched by the child thread's bind.
    assert "self_heal" not in dict(structlog.contextvars.get_contextvars())


def test_wave_run_failed_log_carries_self_heal_marker(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: Any) -> None:
            calls.append((event, kwargs))

        def info(self, *args: Any, **kwargs: Any) -> None:
            pass

    starter = _starter([], raises="fp1")

    original_log = remediate_mod.log
    remediate_mod.log = _FakeLog()
    try:
        run_fix_wave(
            _report([_cluster("fp1", "AError")]),
            settings=_settings(),
            runs_dir=tmp_path,
            start_run=starter,
        )
    finally:
        remediate_mod.log = original_log

    failed_calls = [c for c in calls if c[0] == "selfheal.remediate.run_failed"]
    assert len(failed_calls) == 1
    assert failed_calls[0][1]["self_heal"] is True


def test_escalate_failed_log_carries_self_heal_marker() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeLog:
        def error(self, event: str, **kwargs: Any) -> None:
            calls.append((event, kwargs))

    class _Boom:
        def notify(self, message: str) -> bool:
            raise RuntimeError("slack down")

    original_log = remediate_mod.log
    remediate_mod.log = _FakeLog()
    try:
        escalate(_report([_cluster("fp1", "AError")]), [], _Boom())
    finally:
        remediate_mod.log = original_log

    failed_calls = [c for c in calls if c[0] == "selfheal.remediate.escalate_failed"]
    assert len(failed_calls) == 1
    assert failed_calls[0][1]["self_heal"] is True


# ── loop closure: the wave's own run files must never feed the scanner ────


def test_wave_run_file_is_skipped_by_scan_day(tmp_path: Path) -> None:
    """End-to-end proof the self-healing loop closes.

    A run the wave launched writes ordinary error/llm.call/verify events like
    any other run. If ``scan_day`` mined them, tomorrow's digest would contain
    the loop's own remediation noise and it would try to "fix" itself forever.
    Task 2's guard keys off ``pipeline.start``'s ``self_heal`` payload flag —
    which is exactly what the wave stamps here, with no test-only plumbing.
    """
    runs_dir = tmp_path / "runs"
    report = _report([_cluster("fp1", "ImportError", samples=["ImportError: no module named x"])])

    outcomes = run_fix_wave(report, settings=_settings(), runs_dir=runs_dir)
    assert outcomes[0].run_id is not None

    today = datetime.now(UTC).date().isoformat()
    digest = scan_day(
        today,
        selflogs_dir=tmp_path / "selflogs",
        runs_dir=runs_dir,
        out_dir=tmp_path / "selfheal",
    )

    assert digest.skipped_self_runs == 1
    assert digest.scanned_runs == 0
    assert digest.clusters == []


def test_wave_run_that_dies_before_pipeline_start_is_still_skipped_by_scan_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nastiest corner of the loop guard.

    If the run body blows up before ``pipeline.run`` ever emits
    ``pipeline.start`` (here: ``build_pipeline`` itself raises), the run file
    contains ONLY the seam's ``error`` event. Unless that event also carries
    the ``self_heal`` marker, the next daily scan mines the wave's own crash as
    a fresh failure — and the loop starts chasing its own tail.
    """
    runs_dir = tmp_path / "runs"

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("build_pipeline exploded")

    monkeypatch.setattr("tvastr.selfheal.remediate.build_pipeline", _boom)

    outcomes = run_fix_wave(
        _report([_cluster("fp1", "AError")]), settings=_settings(), runs_dir=runs_dir
    )
    assert outcomes[0].status == "failed"

    today = datetime.now(UTC).date().isoformat()
    digest = scan_day(
        today,
        selflogs_dir=tmp_path / "selflogs",
        runs_dir=runs_dir,
        out_dir=tmp_path / "selfheal",
    )

    assert digest.skipped_self_runs == 1
    assert digest.clusters == []


# ── escalation ────────────────────────────────────────────────────────────


def test_escalation_message_has_all_three_sections_and_sends_once() -> None:
    report = _report(
        [_cluster("fp1", "AError", count=12), _cluster("fp2", "BError", count=5)],
        [_cluster("fp9", "CError", count=3)],
    )
    url = f"https://github.com/{SELF_REPO}/pull/7"
    outcomes = [
        FixOutcome("fp1", "AError", "run1", "pr_created", url),
        FixOutcome("fp2", "BError", "run2", "unverified", None),
    ]
    notifier = MockSlackNotifier()

    message = escalate(report, outcomes, notifier)

    assert notifier.sent == [message]
    assert "2026-W34" in message
    for heading in ("Fixed", "Not fixed", "Report-only"):
        assert heading in message
    # No cluster may go silently missing from the summary.
    for cluster in report.to_fix + report.report_only:
        assert cluster.title in message
    assert url in message
    assert "run2" in message  # a run link for the unfixed cluster
    assert "12" in message and "5" in message and "3" in message  # counts
    # Every to_fix cluster's fingerprint token must appear, fixed or not.
    for cluster in report.to_fix:
        assert f"`{cluster.fingerprint[:12]}`" in message


def test_escalation_reports_unattempted_clusters_when_the_wave_produced_nothing() -> None:
    """The scheduler still escalates when the fix wave blew up — every to_fix
    cluster must then be reported as unattempted, not silently dropped."""
    report = _report([_cluster("fp1", "AError"), _cluster("fp2", "BError")])
    notifier = MockSlackNotifier()

    message = escalate(report, [], notifier)

    assert "AError" in message and "BError" in message
    assert message.count("skipped") >= 2
    assert len(notifier.sent) == 1
    for cluster in report.to_fix:
        assert f"`{cluster.fingerprint[:12]}`" in message


def test_escalation_fingerprint_token_appears_in_every_section() -> None:
    """FINDING 2: build_escalation renders clusters by title only, with no way
    to correlate a Slack line back to the cluster it came from. Every line in
    every section (fixed/unfixed/report-only) must carry a short fingerprint
    token, format exactly `` `{fingerprint[:12]}` ``."""
    report = _report(
        [_cluster("aaaaaaaaaaaaaaaa", "AError"), _cluster("bbbbbbbbbbbbbbbb", "BError")],
        [_cluster("cccccccccccccccc", "CError")],
    )
    outcomes = [
        FixOutcome("aaaaaaaaaaaaaaaa", "AError", "run1", "pr_created", "https://example/pr/1"),
        FixOutcome("bbbbbbbbbbbbbbbb", "BError", "run2", "unverified", None),
    ]

    message = build_escalation(report, outcomes)

    assert "`aaaaaaaaaaaa`" in message  # fixed line
    assert "`bbbbbbbbbbbb`" in message  # unfixed line
    assert "`cccccccccccc`" in message  # report-only line


def test_escalation_includes_outcomes_absent_from_the_report() -> None:
    report = _report([])
    notifier = MockSlackNotifier()

    message = escalate(report, [FixOutcome("fpX", "OrphanError", "runX", "failed", None)], notifier)

    assert "OrphanError" in message
    assert len(notifier.sent) == 1


def test_escalation_survives_a_notifier_failure_and_still_returns_the_text() -> None:
    class _Boom:
        def notify(self, message: str) -> bool:
            raise RuntimeError("slack down")

    message = escalate(_report([_cluster("fp1", "AError")]), [], _Boom())

    assert "AError" in message


# ── scheduler wiring: consolidate -> fix wave -> escalate ─────────────────


def _scheduler(tmp_path: Path, **kwargs: Any):  # type: ignore[no-untyped-def]
    from tvastr.selfheal.scheduler import SelfHealScheduler

    return SelfHealScheduler(
        settings=_settings(self_heal_weekly_day=7, self_heal_daily_hour=2),
        root=tmp_path,
        now_fn=lambda: datetime(2026, 8, 23, 2, 0, tzinfo=UTC),
        **kwargs,
    )


def _seed_digest(tmp_path: Path, day: str) -> None:
    """Write one real daily digest (via scan.py's own writer) so the weekly
    consolidation has something to rank."""
    selflogs_dir = tmp_path / "selflogs"
    selflogs_dir.mkdir(parents=True, exist_ok=True)
    (selflogs_dir / f"tvastr-{day}.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "run.failed: RuntimeError: boom",
                    "level": "error",
                    "timestamp": f"{day}T10:00:00+00:00",
                    "logger": "tvastr.api.routes.run",
                }
            )
            for _ in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    scan_day(
        day,
        selflogs_dir=selflogs_dir,
        runs_dir=tmp_path / "runs",
        out_dir=tmp_path / "selfheal",
    )


def test_default_weekly_hook_consolidates_then_fixes_then_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_digest(tmp_path, "2026-08-18")  # a day inside 2026-W34... target is W33
    _seed_digest(tmp_path, "2026-08-12")  # 2026-W33 (Wednesday)
    seen: dict[str, Any] = {}

    def _fake_wave(report: WeeklyReport, **kwargs: Any) -> list[FixOutcome]:
        seen["wave"] = (report, kwargs)
        return [
            FixOutcome(c.fingerprint, c.title, "runZ", "pr_created", "https://example/pr/1")
            for c in report.to_fix
        ]

    def _fake_escalate(report: WeeklyReport, outcomes: list[FixOutcome], notifier: Any) -> str:
        seen["escalate"] = (report, outcomes, notifier)
        return "msg"

    monkeypatch.setattr("tvastr.selfheal.scheduler.run_fix_wave", _fake_wave)
    monkeypatch.setattr("tvastr.selfheal.scheduler.escalate", _fake_escalate)

    fired = _scheduler(tmp_path).tick()

    assert "weekly:2026-W33" in fired
    assert (tmp_path / "selfheal" / "weekly" / "2026-W33.json").exists()
    report, kwargs = seen["wave"]
    assert report.week == "2026-W33"
    assert report.to_fix  # the seeded W33 digest produced a fix candidate
    assert kwargs["runs_dir"] == tmp_path / "runs"
    assert kwargs["settings"].self_heal_repo == SELF_REPO
    esc_report, esc_outcomes, _ = seen["escalate"]
    assert esc_report is report
    assert [o.fingerprint for o in esc_outcomes] == [c.fingerprint for c in report.to_fix]


def test_default_weekly_hook_skips_escalation_when_there_is_nothing_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _record_escalate(*a: Any, **k: Any) -> str:
        calls.append("escalate")
        return "msg"

    monkeypatch.setattr("tvastr.selfheal.scheduler.escalate", _record_escalate)

    _scheduler(tmp_path).tick()  # no digests at all -> empty report

    assert calls == []
    assert (tmp_path / "selfheal" / "weekly" / "2026-W33.json").exists()


def test_default_weekly_hook_skips_wave_and_escalation_when_consolidation_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _boom(*a: Any, **k: Any) -> WeeklyReport:
        raise RuntimeError("consolidation exploded")

    def _record_wave(*a: Any, **k: Any) -> list[FixOutcome]:
        calls.append("wave")
        return []

    def _record_escalate(*a: Any, **k: Any) -> str:
        calls.append("escalate")
        return "msg"

    monkeypatch.setattr("tvastr.selfheal.scheduler.consolidate_week", _boom)
    monkeypatch.setattr("tvastr.selfheal.scheduler.run_fix_wave", _record_wave)
    monkeypatch.setattr("tvastr.selfheal.scheduler.escalate", _record_escalate)

    fired = _scheduler(tmp_path).tick()  # must not raise

    assert "weekly:2026-W33" in fired
    assert calls == []


def test_default_weekly_hook_still_escalates_when_the_wave_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_digest(tmp_path, "2026-08-12")
    seen: dict[str, Any] = {}

    def _boom(*a: Any, **k: Any) -> list[FixOutcome]:
        raise RuntimeError("wave exploded")

    def _record_escalate(report: WeeklyReport, outcomes: list[FixOutcome], notifier: Any) -> str:
        seen.update(report=report, outcomes=outcomes)
        return "msg"

    monkeypatch.setattr("tvastr.selfheal.scheduler.run_fix_wave", _boom)
    monkeypatch.setattr("tvastr.selfheal.scheduler.escalate", _record_escalate)

    _scheduler(tmp_path).tick()  # must not raise

    assert seen["outcomes"] == []
    assert seen["report"].to_fix  # escalated with everything unattempted
