"""The self-remediation fix wave: run tvastr's own top failures through tvastr.

This is where the self-healing loop stops being observation and becomes action.
:func:`run_fix_wave` takes a week's :class:`~tvastr.selfheal.report.WeeklyReport`
and, for each ``to_fix`` cluster, reconstructs representative
:class:`~tvastr.domain.LogEvent`\\ s and drives them through the *same*
:class:`~tvastr.pipeline.RemediationPipeline` the API route uses — only pointed
at tvastr's own repo (``settings.self_heal_repo``) instead of the testbed repo.
:func:`escalate` then posts one Slack summary of what got fixed, what didn't,
and what was merely observed.

Three constraints shape the implementation:

**Sequential, never concurrent.** Every self-fix works on the same repo; two
overlapping runs would race on branches and on each other's reads. The wave
therefore joins each pipeline thread before starting the next one. That is why
``start_run`` is *synchronous* by contract (it returns only once the run has
finished) rather than returning a handle to join later — a fire-and-forget
starter simply cannot be plugged in by mistake.

**The loop guard is the point.** ``run_meta`` carries ``self_heal: True``, and
``pipeline.py`` spreads ``run_meta`` flat into ``pipeline.start``'s payload —
exactly where ``scan.py``'s guard looks. Without that key the wave's own runs
would be mined as fresh failures by the next daily scan and the loop would feed
on itself forever. ``tests/test_selfheal_remediate.py`` pins the closure
end-to-end (wave writes a run file -> ``scan_day`` skips it).

**Outcomes are read back from the run file, not from the pipeline object.** The
JSONL is the single source of truth for a run (same as the stream endpoint),
and reading it back is what an injected ``start_run`` can also produce in a
test. Terminality is judged by ANY terminal event in the file, never the last
line, because verify events append after ``pipeline.end`` (see CLAUDE.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

from tvastr.config import Settings
from tvastr.domain import LogEvent, Severity
from tvastr.events import (
    EventSink,
    JsonlEventSink,
    PipelineEvent,
    is_terminal_event,
    load_events,
    new_run_id,
    run_path,
)
from tvastr.ingestion import SimulatedLogSource
from tvastr.logging import get_logger
from tvastr.pipeline import build_pipeline
from tvastr.runner import start_pipeline_thread
from tvastr.selfheal.report import RankedCluster, WeeklyReport

log = get_logger(__name__)

#: A PR was demonstrably opened for the cluster.
STATUS_PR_CREATED = "pr_created"
#: The run finished but produced no PR (low confidence, nothing to change, dry run).
STATUS_UNVERIFIED = "unverified"
#: The run errored, never reached a terminal event, or could not be started.
STATUS_FAILED = "failed"
#: No run was attempted for this cluster at all.
STATUS_SKIPPED = "skipped"

# Reconstructed self-heal events are stamped with tvastr's own service/source so
# they are visibly distinct from target-repo signals anywhere they surface.
_SELF_SERVICE = "tvastr-self"
_SELF_SOURCE = "selfheal"


@dataclass(frozen=True)
class FixOutcome:
    """What the wave achieved for one ``to_fix`` cluster."""

    fingerprint: str
    title: str
    run_id: str | None
    status: str
    pr_url: str | None
    # ALL pr urls opened during the run (one cluster can open more than one PR:
    # cluster_events reconstructs one LogEvent per surviving sample message,
    # and dissimilar samples can fingerprint into separate detected patterns —
    # each gets its own agent run and PR). ``pr_url`` stays the first for
    # interface stability; this is additive so existing positional/keyword
    # FixOutcome(...) callers (tests included) are unaffected.
    pr_urls: tuple[str, ...] = ()


def _outcomes_path(out_dir: Path, week: str) -> Path:
    return out_dir / "weekly" / f"{week}-outcomes.json"


def write_outcomes(week: str, outcomes: list[FixOutcome], out_dir: Path) -> None:
    """Persist a week's fix-wave outcomes alongside its ``WeeklyReport``.

    Outcomes were never persisted before Task 6 (``run_fix_wave`` returns them
    in-process only, to the scheduler's weekly hook). Task 6's report route
    needs them back on a later request to merge PR/status info onto each
    ``to_fix`` cluster, so the weekly hook writes this file additively --
    ``run_fix_wave``'s own signature is untouched. Written next to
    ``out_dir/weekly/{week}.json`` (the report itself) as
    ``out_dir/weekly/{week}-outcomes.json``.
    """
    path = _outcomes_path(out_dir, week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(o) for o in outcomes], indent=2), encoding="utf-8")


def load_outcomes(week: str, out_dir: Path) -> list[FixOutcome] | None:
    """Load a week's persisted fix-wave outcomes.

    ``None`` if no wave was ever recorded for this week -- distinct from an
    empty list (a wave ran and produced zero ``to_fix`` outcomes).
    """
    path = _outcomes_path(out_dir, week)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    outcomes: list[FixOutcome] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        outcomes.append(
            FixOutcome(
                fingerprint=row["fingerprint"],
                title=row["title"],
                run_id=row.get("run_id"),
                status=row["status"],
                pr_url=row.get("pr_url"),
                pr_urls=tuple(row.get("pr_urls", ())),
            )
        )
    return outcomes


class _Notifier(Protocol):
    def notify(self, message: str) -> bool: ...


class StartRun(Protocol):
    """Start ONE self-heal pipeline run and return only when it has finished.

    The default implementation (:func:`_default_start_run`) runs the real
    pipeline on a background thread and joins it. Tests inject a fake that
    records the call and writes a canned run file through ``sink``.
    """

    def __call__(
        self,
        *,
        events: list[LogEvent],
        run_meta: dict[str, Any],
        settings: Settings,
        sink: EventSink,
        run_id: str,
    ) -> str: ...


def _parse_seen(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def cluster_events(cluster: RankedCluster) -> list[LogEvent]:
    """Reconstruct representative ``LogEvent``\\ s for one ranked cluster.

    A ``RankedCluster`` is a *summary*: only up to three sample messages
    survived the digest -> weekly-report round trip (see ``scan.py``'s
    ``sample_messages``). Those messages plus the cluster's first/last-seen
    timestamps are all the evidence there is, so they are what the agent gets.
    A cluster whose samples were all lost still gets one event built from its
    title, so it is diagnosed rather than silently dropped.
    """
    messages = [m for m in cluster.sample_messages if m and m.strip()] or [cluster.title]
    first, last = _parse_seen(cluster.first_seen), _parse_seen(cluster.last_seen)
    events: list[LogEvent] = []
    for index, message in enumerate(messages):
        # First/last-seen bracket the cluster's real lifetime; the last sample
        # carries last_seen so issue-era retrieval (if on) resolves against the
        # most recent occurrence rather than the oldest.
        stamp = last if (index == len(messages) - 1 and last is not None) else first
        events.append(
            LogEvent(
                **({"timestamp": stamp} if stamp is not None else {}),
                service=_SELF_SERVICE,
                severity=Severity.ERROR,
                message=message,
                attributes={
                    "origin": _SELF_SOURCE,
                    "fingerprint": cluster.fingerprint,
                },
                source=_SELF_SOURCE,
            )
        )
    return events


def _self_run_settings(settings: Settings) -> Settings:
    """Point the run at tvastr's OWN repo and bypass the recurrence threshold.

    The cluster has already recurred (that is what put it in ``to_fix``), so
    re-applying the threshold to its handful of reconstructed sample events
    would select nothing. Same reasoning as the API route's single-issue mode.

    ``dry_run`` is forced True unless ``self_heal_open_prs`` is explicitly on:
    the wave runs autonomously (no human picks the issue, unlike the API
    route), so opening real PRs against ``self_heal_repo`` must be an opt-in,
    not something that happens the moment self-heal is enabled in live mode.
    """
    update: dict[str, Any] = {"github_repo": settings.self_heal_repo, "recurrence_threshold": 1}
    if not settings.self_heal_open_prs:
        update["dry_run"] = True
    return settings.model_copy(update=update)


def _default_start_run(
    *,
    events: list[LogEvent],
    run_meta: dict[str, Any],
    settings: Settings,
    sink: EventSink,
    run_id: str,
) -> str:
    """Run the real pipeline on a background thread and JOIN it before returning."""

    def _body() -> None:
        # Contextvars don't propagate into new threads (this body runs on one),
        # so the marker must be bound HERE, not in the wave's loop. Without it,
        # any log this thread emits mid-run (not just the seam's terminal
        # error, which gets error_payload separately) lands in the selflog
        # file unmarked and scan.py's selflog guard never fires on it.
        structlog.contextvars.bind_contextvars(self_heal=True)
        try:
            pipeline = build_pipeline(
                settings,
                log_source=SimulatedLogSource(),  # unused; events passed directly below
                event_sink=sink,
                run_id=run_id,
            )
            pipeline.threshold.recurrence_threshold = 1
            pipeline.run(events=events, run_meta=run_meta)
        finally:
            structlog.contextvars.clear_contextvars()

    # error_payload: if the body dies before pipeline.start is emitted, the only
    # event in the file is the failure — it must still carry the self_heal
    # marker or the next daily scan mines the wave's own crash (see runner.py).
    thread = start_pipeline_thread(
        _body, run_id=run_id, sink=sink, error_payload={"self_heal": True}
    )
    thread.join()
    return run_id


def _pr_urls(events: list[PipelineEvent]) -> tuple[str, ...]:
    """ALL PR urls opened during a run's events, in emission order, deduped.

    Verified against the emission sites, not guessed: ``agent/graph.py``'s
    ``_open_pr`` emits ``pr.opened`` with ``url``/``number`` (once per detected
    pattern -- a run can open more than one PR), and ``pipeline.py`` emits
    ``audit.saved`` with ``pull_request_url`` (which is ``None`` in dry-run,
    where the URL is only a sentinel). ``pr.dry_run`` is deliberately NOT
    treated as a PR — nothing was opened.
    """
    urls: list[str] = []
    for event in events:
        if event.type == "pr.opened":
            url = event.payload.get("url")
        elif event.type == "audit.saved":
            url = event.payload.get("pull_request_url")
        else:
            continue
        if url and str(url) not in urls:
            urls.append(str(url))
    return tuple(urls)


def _read_outcome(cluster: RankedCluster, run_id: str, runs_dir: Path) -> FixOutcome:
    events = list(load_events(run_path(run_id, runs_dir)))
    urls = _pr_urls(events)
    url = urls[0] if urls else None
    # A PR wins over a later error: the run demonstrably produced the artifact
    # this wave exists to produce, and reporting that as "failed" would hide it
    # from the escalation summary (and from whoever has to review the PR).
    if url is not None:
        status = STATUS_PR_CREATED
    elif any(e.type == "error" for e in events) or not any(is_terminal_event(e) for e in events):
        # ANY terminal event, never the last line — verify events append after
        # pipeline.end (CLAUDE.md). No terminal event at all means the run died
        # without even the seam's error event: failed, not "finished quietly".
        status = STATUS_FAILED
    else:
        status = STATUS_UNVERIFIED
    return FixOutcome(
        fingerprint=cluster.fingerprint,
        title=cluster.title,
        run_id=run_id,
        status=status,
        pr_url=url,
        pr_urls=urls,
    )


def _skipped_outcomes(clusters: list[RankedCluster]) -> list[FixOutcome]:
    return [FixOutcome(c.fingerprint, c.title, None, STATUS_SKIPPED, None) for c in clusters]


def run_fix_wave(
    report: WeeklyReport,
    *,
    settings: Settings,
    runs_dir: Path,
    start_run: Callable[..., str] | None = None,
) -> list[FixOutcome]:
    """Attempt a fix for every ``to_fix`` cluster, one run at a time.

    Returns one :class:`FixOutcome` per ``to_fix`` cluster, in report order. A
    cluster whose run cannot even be started is reported ``failed`` and the wave
    moves on — one poisoned cluster must not cost the others their fix attempt.

    This function itself must never raise: every step for one cluster (id
    generation, run-file pre-creation, starting the run, reading the outcome
    back) lives inside that cluster's own try/except, so a failure anywhere in
    that sequence downgrades only THAT cluster to ``failed`` — it can never
    discard an already-completed outcome for an earlier cluster (e.g. a real
    opened PR silently becoming "skipped"). A catastrophic failure before any
    cluster is even attempted (settings prep) still returns one outcome per
    ``to_fix`` cluster, reported ``skipped``, rather than raising out of the
    weekly job.
    """
    outcomes: list[FixOutcome] = []
    try:
        starter: Callable[..., str] = start_run or _default_start_run
        run_settings = _self_run_settings(settings)
        mode = "mock" if settings.use_mocks else "live"

        for cluster in report.to_fix:
            run_id: str | None = None
            try:
                run_id = new_run_id()
                # Pre-create the run file (same reason as the API route: a
                # reader must never see "unknown run" for a run that is, in
                # fact, running).
                path = run_path(run_id, runs_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                run_meta: dict[str, Any] = {
                    "run_id": run_id,
                    # THE loop guard: pipeline.py spreads run_meta flat into
                    # pipeline.start's payload, which is where scan.py looks.
                    "self_heal": True,
                    "fingerprint": cluster.fingerprint,
                    "cluster_title": cluster.title,
                    "repo": run_settings.github_repo,
                    "mode": mode,
                }
                log.info(
                    "selfheal.remediate.run_start",
                    week=report.week,
                    run_id=run_id,
                    fingerprint=cluster.fingerprint,
                    repo=run_settings.github_repo,
                )
                started_id = starter(
                    events=cluster_events(cluster),
                    run_meta=run_meta,
                    settings=run_settings,
                    sink=JsonlEventSink(path),
                    run_id=run_id,
                )
                outcome = _read_outcome(cluster, started_id or run_id, runs_dir)
            except Exception as exc:
                log.error(
                    "selfheal.remediate.run_failed",
                    run_id=run_id,
                    fingerprint=cluster.fingerprint,
                    error=str(exc),
                    self_heal=True,
                )
                outcome = FixOutcome(
                    cluster.fingerprint, cluster.title, run_id, STATUS_FAILED, None
                )
            else:
                log.info(
                    "selfheal.remediate.run_done",
                    run_id=outcome.run_id,
                    fingerprint=outcome.fingerprint,
                    status=outcome.status,
                )
            outcomes.append(outcome)
    except Exception as exc:
        log.error("selfheal.remediate.wave_failed", error=str(exc), self_heal=True)
        attempted = {o.fingerprint for o in outcomes}
        outcomes.extend(
            _skipped_outcomes([c for c in report.to_fix if c.fingerprint not in attempted])
        )

    return outcomes


# ── escalation ────────────────────────────────────────────────────────────


def _run_link(run_id: str | None) -> str:
    """A link path to the run, not a filesystem path.

    ``FixOutcome`` carries only the run id, and the wave's ``runs_dir`` is not
    part of :func:`escalate`'s signature, so a ``data/runs/...`` path would be a
    guess that is wrong for any non-default runs dir. The API route that serves
    the run is exact and clickable behind the UI's host.
    """
    return f"/api/runs/{run_id}" if run_id else "(no run)"


def _fingerprint_token(fingerprint: str) -> str:
    """Short, clickable-in-spirit correlation token for a Slack line.

    Every cluster line in every section carries this so a reader (or a
    follow-up automation) can tie a Slack line back to the exact cluster,
    without repeating the full fingerprint hash inline.
    """
    return f"`{fingerprint[:12]}`"


def _fixed_line(outcome: FixOutcome, cluster: RankedCluster | None) -> str:
    count = f" ({cluster.count}x)" if cluster else ""
    # pr_urls is the source of truth when populated (one cluster can open more
    # than one PR); outcomes built without it (older call sites, tests) fall
    # back to the single pr_url so no caller is left with a blank line.
    urls = outcome.pr_urls or ((outcome.pr_url,) if outcome.pr_url else ())
    urls_text = ", ".join(urls) if urls else str(outcome.pr_url)
    return f"• {outcome.title}{count} — {urls_text} — {_fingerprint_token(outcome.fingerprint)}"


def _unfixed_line(outcome: FixOutcome, cluster: RankedCluster | None) -> str:
    count = f" ({cluster.count}x)" if cluster else ""
    return (
        f"• {outcome.title}{count} — {outcome.status} — {_run_link(outcome.run_id)}"
        f" — {_fingerprint_token(outcome.fingerprint)}"
    )


def _section(heading: str, lines: list[str]) -> list[str]:
    return [f"*{heading} ({len(lines)})*", *(lines or ["• (none)"])]


def build_escalation(report: WeeklyReport, outcomes: list[FixOutcome]) -> str:
    """Render the weekly self-heal summary. Pure; :func:`escalate` sends it.

    Every ``to_fix`` cluster appears exactly once, whether or not the wave got
    to it: a cluster with no outcome is reported ``skipped`` (unattempted)
    rather than vanishing — which is what makes the summary honest when the
    wave itself blew up and ``outcomes`` is empty.
    """
    by_fingerprint = {c.fingerprint: c for c in report.to_fix}
    outcome_by_fingerprint = {o.fingerprint: o for o in outcomes}

    attempted: list[FixOutcome] = [
        outcome_by_fingerprint.get(
            cluster.fingerprint,
            FixOutcome(cluster.fingerprint, cluster.title, None, STATUS_SKIPPED, None),
        )
        for cluster in report.to_fix
    ]
    # Defensive: an outcome for a cluster that is not in to_fix (shouldn't
    # happen) is still reported rather than dropped on the floor.
    attempted += [o for o in outcomes if o.fingerprint not in by_fingerprint]

    fixed = [o for o in attempted if o.status == STATUS_PR_CREATED]
    unfixed = [o for o in attempted if o.status != STATUS_PR_CREATED]
    total = len(report.to_fix) + len(report.report_only)

    lines = [
        f":robot_face: tvastr self-heal — week {report.week}",
        (
            f"{total} ranked cluster(s): {len(report.to_fix)} fix candidate(s), "
            f"{len(report.report_only)} report-only "
            f"({len(report.days_scanned)} day(s) scanned)."
        ),
        "",
        *_section("Fixed", [_fixed_line(o, by_fingerprint.get(o.fingerprint)) for o in fixed]),
        "",
        *_section(
            "Not fixed", [_unfixed_line(o, by_fingerprint.get(o.fingerprint)) for o in unfixed]
        ),
        "",
        *_section(
            "Report-only",
            [
                f"• {c.title} ({c.count}x, {c.kind}) — {_fingerprint_token(c.fingerprint)}"
                for c in report.report_only
            ],
        ),
    ]
    return "\n".join(lines)


def escalate(report: WeeklyReport, outcomes: list[FixOutcome], notifier: _Notifier) -> str:
    """Send ONE Slack summary of the week's self-heal wave; return its text.

    A failing notifier is logged, never raised: the summary text is the
    caller's return value (and the tested artifact), and losing the Slack post
    must not take down the weekly job that produced it.
    """
    message = build_escalation(report, outcomes)
    try:
        notifier.notify(message)
    except Exception as exc:
        log.error(
            "selfheal.remediate.escalate_failed", week=report.week, error=str(exc), self_heal=True
        )
    return message
