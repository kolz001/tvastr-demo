"""Daily digest scanner: turn one day's selflogs + run files into fingerprinted clusters.

This is where the self-healing loop's most important invariant lives: a self-run
(a remediation run the loop itself launched to fix a *previous* self-heal issue)
must never feed candidates back into today's digest. Without that guard the loop
could observe its own failures, "fix" them, launch a run to verify the fix, have
that run itself emit ordinary error/llm.call events, and re-detect those as new
failures forever. ``collect_candidates`` enforces the guard by skipping the whole
run file (not just self_heal-tagged events within it) whenever ``pipeline.start``'s
payload carries a truthy ``self_heal`` key — see ``api/routes/run.py``'s
``run_meta``, which ``pipeline.py`` spreads directly into the event's payload.

Every function here is pure: given directories and a day string, read-only glob +
parse, no mutation of ``data/``. ``scan_day`` is the only function that writes
(to ``out_dir``, never to the selflogs/runs directories it reads from).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tvastr.detection.detector import FailureDetector
from tvastr.domain import FailurePattern, LogEvent, Severity
from tvastr.events import PipelineEvent, is_terminal_event, load_events
from tvastr.selfheal.selflog import selflog_path

# tvastr.verification.models.Verdict.REPRO_BROKEN.value — duplicated as a literal
# rather than imported so this pure/lightweight scanner doesn't pull in the
# verification package's much heavier import graph (sandbox, agent context, ...).
_REPRO_BROKEN = "repro_broken"

_ORIGIN_SELFLOG = "selflog"
_ORIGIN_RUNS = "runs"


@dataclass(frozen=True)
class DailyDigest:
    """One day's mined candidates, clustered into failure patterns.

    ``events`` is the full candidate list that fed ``clusters`` for the run that
    produced this digest in-process. ``load_daily`` cannot reconstruct that list
    from disk (only up to 3 sample messages per cluster are persisted), so a
    digest loaded back from disk always has ``events == []``; callers that need
    per-cluster samples should read them from the persisted JSONL directly.
    """

    day: str
    clusters: list[FailurePattern]
    events: list[LogEvent]
    scanned_runs: int
    skipped_self_runs: int


def _selflog_candidates(day: str, selflogs_dir: Path) -> list[LogEvent]:
    """Mine one day's selflog file for error/critical records worth surfacing."""
    path = selflog_path(selflogs_dir, day)
    if not path.exists():
        return []

    candidates: list[LogEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("level") not in {"error", "critical"}:
                continue
            if record.get("self_heal"):
                continue
            message = record.get("event")
            if not message:
                continue
            stack = record.get("exception") or record.get("stack")
            candidates.append(
                LogEvent(
                    service="tvastr-self",
                    severity=Severity.ERROR,
                    message=str(message),
                    stack_trace=str(stack) if stack else None,
                    attributes={
                        "origin": _ORIGIN_SELFLOG,
                        "logger": str(record.get("logger", "")),
                    },
                    source="selfheal",
                )
            )
    return candidates


def _stringify(value: Any) -> str | None:
    return None if value is None else str(value)


def _issue_ref(events: list[PipelineEvent]) -> str:
    """Best-effort ``repo#issue_number`` label for quality-signal candidates."""
    for event in events:
        if event.type == "pipeline.start":
            repo = event.payload.get("repo")
            issue_number = event.payload.get("issue_number")
            if repo is not None and issue_number is not None:
                return f"{repo}#{issue_number}"
            if repo is not None:
                return str(repo)
            if issue_number is not None:
                return str(issue_number)
    return ""


def _run_attributes(run_id: str, issue_ref: str) -> dict[str, str]:
    return {"origin": _ORIGIN_RUNS, "run_id": run_id, "issue": issue_ref}


def _ops_log_event(run_id: str, issue_ref: str, event: PipelineEvent) -> LogEvent:
    payload = event.payload
    reason = payload.get("reason") or payload.get("error") or payload.get("message") or event.step
    return LogEvent(
        service="tvastr-runs",
        severity=Severity.ERROR,
        message=f"{event.step}: {reason}",
        stack_trace=_stringify(payload.get("stack_trace") or payload.get("traceback")),
        attributes=_run_attributes(run_id, issue_ref),
        source="selfheal",
    )


def _confidence_zero_log_event(run_id: str, issue_ref: str) -> LogEvent:
    return LogEvent(
        service="tvastr-runs",
        severity=Severity.ERROR,
        message="investigator returned confidence 0.0",
        attributes=_run_attributes(run_id, issue_ref),
        source="selfheal",
    )


def _repro_broken_log_event(run_id: str, issue_ref: str, event: PipelineEvent) -> LogEvent:
    return LogEvent(
        service="tvastr-runs",
        severity=Severity.ERROR,
        message="verify verdict repro_broken",
        stack_trace=_stringify(event.payload.get("rerun_stderr_tail")),
        attributes=_run_attributes(run_id, issue_ref),
        source="selfheal",
    )


def _llm_call_failed(payload: dict[str, Any]) -> bool:
    """No real emission site currently sets an error/success field on llm.call
    (a failed ``client.complete()`` call raises and is caught by the caller
    without an llm.call event at all — see ``llm/router.py``). This checks for
    ``error`` / ``success: False`` defensively so a future emitter that *does*
    record failures is picked up without a scan.py change."""
    if payload.get("success") is False:
        return True
    return bool(payload.get("error"))


def _llm_failure_log_event(run_id: str, issue_ref: str, event: PipelineEvent) -> LogEvent:
    payload = event.payload
    detail = payload.get("error") or "llm call failed"
    task = payload.get("task", "unknown")
    return LogEvent(
        service="tvastr-runs",
        severity=Severity.ERROR,
        message=f"llm.call failed ({task}): {detail}",
        attributes=_run_attributes(run_id, issue_ref),
        source="selfheal",
    )


def _extract_run_candidates(run_id: str, events: list[PipelineEvent]) -> list[LogEvent]:
    issue_ref = _issue_ref(events)
    candidates: list[LogEvent] = []
    last_investigate: PipelineEvent | None = None

    for event in events:
        if event.type == "error":
            candidates.append(_ops_log_event(run_id, issue_ref, event))
        elif event.type == "agent.node.end" and event.step == "investigate":
            last_investigate = event
        elif event.type == "verify.result" and event.payload.get("verdict") == _REPRO_BROKEN:
            candidates.append(_repro_broken_log_event(run_id, issue_ref, event))
        elif event.type == "llm.call" and _llm_call_failed(event.payload):
            candidates.append(_llm_failure_log_event(run_id, issue_ref, event))

    if last_investigate is not None and last_investigate.payload.get("confidence") == 0.0:
        candidates.append(_confidence_zero_log_event(run_id, issue_ref))

    return candidates


def _run_candidates(day: str, runs_dir: Path) -> tuple[list[LogEvent], int, int]:
    candidates: list[LogEvent] = []
    scanned_runs = 0
    skipped_self_runs = 0

    if not runs_dir.exists():
        return candidates, scanned_runs, skipped_self_runs

    for path in sorted(runs_dir.glob("*.jsonl")):
        events = list(load_events(path))
        if not events:
            continue
        if events[0].timestamp[:10] != day:
            continue
        # Loop guard: a self-heal-launched run tags its pipeline.start run_meta
        # (spread flat into the event's payload by pipeline.py) with a truthy
        # self_heal key. Skip the WHOLE file — never mine a self-run's ordinary
        # error/llm.call/verify events as if they were fresh target-repo failures.
        if any(bool(e.payload.get("self_heal")) for e in events):
            skipped_self_runs += 1
            continue
        # CLAUDE.md invariant: verify.* events append after pipeline.end, so
        # "did this run finish" must scan for ANY terminal event, never just the
        # last line. A run that never reached a terminal event is still
        # in-flight/crashed-without-a-terminal-marker; its quality signals
        # (final confidence, verify verdict) aren't settled yet, so skip it
        # rather than mining a partial state.
        if not any(is_terminal_event(e) for e in events):
            continue
        scanned_runs += 1
        candidates.extend(_extract_run_candidates(path.stem, events))

    return candidates, scanned_runs, skipped_self_runs


def collect_candidates(
    day: str, selflogs_dir: Path, runs_dir: Path
) -> tuple[list[LogEvent], int, int]:
    """Pure candidate collection: selflog + run-file mining for one day.

    Returns ``(events, scanned_runs, skipped_self_runs)``. Never reads or writes
    anything outside ``selflogs_dir``/``runs_dir``.
    """
    events = _selflog_candidates(day, selflogs_dir)
    run_events, scanned_runs, skipped_self_runs = _run_candidates(day, runs_dir)
    events.extend(run_events)
    return events, scanned_runs, skipped_self_runs


def _digest_path(out_dir: Path, day: str) -> Path:
    return out_dir / "daily" / f"{day}.jsonl"


def _write_digest(digest: DailyDigest, out_dir: Path) -> None:
    path = _digest_path(out_dir, digest.day)
    path.parent.mkdir(parents=True, exist_ok=True)

    events_by_id = {event.id: event for event in digest.events}
    lines = [
        json.dumps(
            {
                "day": digest.day,
                "cluster_count": len(digest.clusters),
                "event_count": len(digest.events),
                "scanned_runs": digest.scanned_runs,
                "skipped_self_runs": digest.skipped_self_runs,
            }
        )
    ]
    for pattern in digest.clusters:
        sample_messages = [
            events_by_id[event_id].message
            for event_id in pattern.sample_event_ids[:3]
            if event_id in events_by_id
        ]
        row = pattern.model_dump(mode="json")
        row["sample_messages"] = sample_messages
        lines.append(json.dumps(row))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan_day(day: str, *, selflogs_dir: Path, runs_dir: Path, out_dir: Path) -> DailyDigest:
    """Mine one day's selflogs + run files, cluster the candidates, persist, return.

    Always writes ``out_dir/daily/{day}.jsonl`` — even on an empty day (proof of
    life: the scanner ran and found nothing, distinct from "the scanner never ran").
    """
    events, scanned_runs, skipped_self_runs = collect_candidates(day, selflogs_dir, runs_dir)
    clusters = FailureDetector().detect(events)
    digest = DailyDigest(
        day=day,
        clusters=clusters,
        events=events,
        scanned_runs=scanned_runs,
        skipped_self_runs=skipped_self_runs,
    )
    _write_digest(digest, out_dir)
    return digest


def load_daily(out_dir: Path, day: str) -> DailyDigest | None:
    """Read back a persisted digest. ``None`` if ``scan_day`` never ran for this day.

    ``events`` on the returned digest is always ``[]`` — see ``DailyDigest``.
    """
    path = _digest_path(out_dir, day)
    if not path.exists():
        return None

    lines = [
        line
        for line in (raw.strip() for raw in path.read_text(encoding="utf-8").splitlines())
        if line
    ]
    if not lines:
        return None

    header = json.loads(lines[0])
    clusters: list[FailurePattern] = []
    for line in lines[1:]:
        row = json.loads(line)
        row.pop("sample_messages", None)
        clusters.append(FailurePattern(**row))

    return DailyDigest(
        day=header["day"],
        clusters=clusters,
        events=[],
        scanned_runs=header["scanned_runs"],
        skipped_self_runs=header["skipped_self_runs"],
    )
