"""Pipeline event sink — instrumentation for the live UI and persisted runs.

Every interesting moment in a pipeline run (a tool call, an LLM call, a routing
decision, a node entering/exiting, an outcome) is published as a
:class:`PipelineEvent`. The sink is pluggable: tests use ``ListEventSink`` to
assert on the sequence; the SSE endpoint fans out to a queue *and* a JSONL file
so the UI streams live and the run is also browsable later.

``NullEventSink`` is the default — anywhere ``event_sink`` is optional, "no
sink" means "no instrumentation overhead".
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from tvastr.logging import get_logger

log = get_logger(__name__)

Layer = Literal["ingestion", "detection", "agent", "output"]
EventType = Literal[
    "pipeline.start",
    "pipeline.end",
    "pipeline.interrupted",
    "ingest.read",
    "detect.cluster",
    "detect.pii",
    "threshold.select",
    "agent.start",
    "agent.node.start",
    "agent.node.end",
    "tool.call",
    "llm.call",
    "router.decide",
    "fix.generated",
    "pr.drafted",
    "pr.dry_run",
    "pr.opened",
    "pr.failed",
    "notify.sent",
    "audit.saved",
    # Verification loop
    "verify.start",
    "verify.repro_synth",
    "verify.baseline",
    "verify.patch_applied",
    "verify.rerun",
    "verify.regression",
    "verify.result",
    "benchmark.compared",
    "benchmark.skipped",
    "doc.grounded",
    "doc.skipped",
    "retrieval.issue_era",
    "error",
]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PipelineEvent:
    type: EventType
    layer: Layer
    step: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utcnow_iso)
    run_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: PipelineEvent) -> None: ...


class NullEventSink:
    """Zero-overhead drop sink — the default when instrumentation is off."""

    def emit(self, event: PipelineEvent) -> None:
        return None


class ListEventSink:
    """Collects events in memory. Used in tests; also useful for replay debugging."""

    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: PipelineEvent) -> None:
        with self._lock:
            self.events.append(event)


class JsonlEventSink:
    """Append-only JSONL writer. One file per run, one event per line."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: PipelineEvent) -> None:
        line = event.to_json() + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)


class FanoutEventSink:
    """Broadcast to multiple sinks (e.g. queue for SSE + JSONL for persistence)."""

    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = sinks

    def emit(self, event: PipelineEvent) -> None:
        # A misbehaving sink (e.g. disconnected SSE client) must never break
        # the pipeline. Drop the event for that sink, keep going for the rest.
        for sink in self.sinks:
            with contextlib.suppress(Exception):
                sink.emit(event)


def new_run_id() -> str:
    """Generate a short, sortable-ish run ID."""
    return uuid4().hex[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Persisted-run helpers
# ─────────────────────────────────────────────────────────────────────────────


def default_runs_dir() -> Path:
    return Path("data/runs")


def run_path(run_id: str, runs_dir: Path | None = None) -> Path:
    return (runs_dir or default_runs_dir()) / f"{run_id}.jsonl"


def _parse_event_line(line: str) -> PipelineEvent | None:
    """Parse one JSONL line into a :class:`PipelineEvent`, or ``None`` if malformed."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
        return PipelineEvent(**obj)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def load_events(path: Path) -> Iterable[PipelineEvent]:
    """Read a persisted run's events back. Skips lines that fail to parse."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            event = _parse_event_line(raw)
            if event is not None:
                yield event


_TERMINAL_TYPES = {"pipeline.end", "pipeline.interrupted", "error"}


def is_terminal_event(event: PipelineEvent) -> bool:
    """True if this event, as the LAST event of a run file, means the run is over."""
    return event.type in _TERMINAL_TYPES


def mark_interrupted_runs(runs_dir: Path, in_flight: Mapping[str, threading.Thread]) -> int:
    """Append ``pipeline.interrupted`` to every non-terminal run file.

    Idempotent and append-only. Files whose run_id is in ``in_flight`` are
    skipped. Returns the number of runs marked. Per-file failures are logged
    and never abort the sweep.
    """
    marked = 0
    for path in sorted(runs_dir.glob("*.jsonl")):
        run_id = path.stem
        if run_id in in_flight:
            continue
        try:
            last = None
            for last in load_events(path):  # noqa: B007 — want the last item
                pass
            if last is None or is_terminal_event(last):
                continue
            JsonlEventSink(path).emit(
                PipelineEvent(
                    type="pipeline.interrupted",
                    layer="pipeline",
                    step="pipeline",
                    run_id=run_id,
                    payload={"reason": "server restarted mid-run"},
                )
            )
            marked += 1
        except Exception as exc:
            log.warning("events.sweep.failed", path=str(path), error=str(exc))
    return marked


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    started_at: str | None
    repo: str | None
    issue_number: int | None
    issue_title: str | None
    outcome: str | None
    event_count: int
    verdict: str | None = None  # populated when a verify.result event is persisted


def list_runs(runs_dir: Path | None = None) -> list[RunSummary]:
    """List persisted runs newest-first by file mtime."""
    base = runs_dir or default_runs_dir()
    if not base.exists():
        return []
    summaries: list[RunSummary] = []
    for jsonl in sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        run_id = jsonl.stem
        started_at: str | None = None
        repo: str | None = None
        issue_number: int | None = None
        issue_title: str | None = None
        outcome: str | None = None
        verdict: str | None = None
        count = 0
        for event in load_events(jsonl):
            count += 1
            if event.type == "pipeline.start":
                started_at = event.timestamp
                repo = event.payload.get("repo")
                issue_number = event.payload.get("issue_number")
                issue_title = event.payload.get("issue_title")
            if event.type == "pr.dry_run":
                outcome = "dry_run"
            elif event.type == "pr.opened":
                outcome = "pr_opened"
            elif event.type == "pr.failed":
                outcome = "failed"
            elif event.type == "pipeline.end" and outcome is None:
                outcome = event.payload.get("outcome", "completed")
            if event.type == "verify.result":
                verdict = event.payload.get("verdict")
        summaries.append(
            RunSummary(
                run_id=run_id,
                started_at=started_at,
                repo=repo,
                issue_number=issue_number,
                issue_title=issue_title,
                outcome=outcome,
                event_count=count,
                verdict=verdict,
            )
        )
    return summaries
