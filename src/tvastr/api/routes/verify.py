"""POST /api/runs/{run_id}/verify — stream a verification of a persisted fix.

Loads the persisted event stream for ``run_id``, reconstructs the issue +
fix proposal + sample event from the events, runs the :class:`Verifier` in a
background thread, and streams the resulting ``verify.*`` events as SSE while
appending them to the *same* JSONL file so the run's record stays whole.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from tvastr.agent.context import AgentContext
from tvastr.config import get_settings
from tvastr.domain import FailurePattern, FileChange, FixProposal, LogEvent, RootCause, Sensitivity
from tvastr.events import (
    EventSink,
    FanoutEventSink,
    JsonlEventSink,
    PipelineEvent,
    load_events,
    run_path,
)
from tvastr.integrations import build_code_host, build_notifier
from tvastr.llm.router import build_router
from tvastr.logging import get_logger
from tvastr.verification import Verifier, build_sandbox

log = get_logger(__name__)

router = APIRouter(tags=["verify"])

_END_SENTINEL: Any = object()


class VerifyRequest(BaseModel):
    issue_body: str | None = Field(
        default=None,
        description=(
            "Original GitHub issue body. Optional; falls back to whatever the "
            "persisted run captured."
        ),
    )


class _QueueEventSink:
    def __init__(self, q: queue.Queue[Any]) -> None:
        self.q = q

    def emit(self, event: PipelineEvent) -> None:
        self.q.put(event)


_Reconstructed = tuple[
    FailurePattern, RootCause, FixProposal, list[LogEvent], str, str | None
]


def _reconstruct_from_run(run_id: str) -> _Reconstructed | None:
    """Rebuild the verifier's inputs from a persisted event stream.

    Returns ``(pattern, root_cause, fix, sample_events, repo, issue_title)`` or
    ``None`` if the run doesn't contain a fix to verify (no ``fix.generated``).
    """
    events = list(load_events(run_path(run_id)))
    if not events:
        return None

    repo: str = ""
    issue_title: str | None = None
    pattern: FailurePattern | None = None
    root_cause: RootCause | None = None
    fix: FixProposal | None = None
    sample_events: list[LogEvent] = []

    for ev in events:
        p = ev.payload or {}
        if ev.type == "pipeline.start" and "repo" in p:
            repo = str(p.get("repo", ""))
            issue_title = p.get("issue_title")
        if ev.type == "ingest.read":
            # Reconstruct a minimal LogEvent from the summary so the reproducer
            # synthesizer has *something* to chew on if Claude is needed.
            for service in p.get("services", []) or []:
                sample_events.append(
                    LogEvent(
                        service=str(service),
                        message=str(p.get("first_message", "")),
                        stack_trace=p.get("first_stack_trace"),
                    )
                )
        if ev.type == "detect.cluster" and p.get("first_pattern"):
            fp = p["first_pattern"]
            pattern = FailurePattern(
                fingerprint=str(fp.get("fingerprint", "")),
                title=str(fp.get("title", "")),
                representative_message=str(fp.get("representative_message", "")),
                exception_type=fp.get("exception_type"),
                count=int(fp.get("count", 1)),
                sensitivity=Sensitivity(fp.get("sensitivity", Sensitivity.INTERNAL.value)),
            )
        if ev.type == "agent.start" and pattern is None:
            # Fallback: build a thin pattern from the agent's start payload.
            pattern = FailurePattern(
                fingerprint=str(p.get("fingerprint", run_id)),
                title=str(p.get("title", issue_title or "")),
                representative_message=str(p.get("title", "")),
                exception_type=p.get("exception_type"),
                count=1,
            )
        if ev.type == "fix.generated":
            file_changes = []
            for path, content in (p.get("patched_files") or {}).items():
                file_changes.append(
                    FileChange(
                        path=path,
                        patched_content=content,
                        rationale=(p.get("rationales") or {}).get(path, ""),
                        diff=(p.get("diffs") or {}).get(path),
                    )
                )
            if not file_changes:
                for path in p.get("files") or []:
                    file_changes.append(FileChange(path=path, patched_content="", rationale=""))
            fix = FixProposal(
                pattern_id=pattern.id if pattern else "",
                summary=str(p.get("summary", "")),
                changes=file_changes,
                test_plan=str(p.get("test_plan", "")),
            )
            if root_cause is None:
                root_cause = RootCause(
                    pattern_id=pattern.id if pattern else "",
                    summary=str(p.get("summary", "")),
                    suspected_files=[c.path for c in file_changes],
                    confidence=0.5,
                )

    if pattern is None or fix is None or root_cause is None:
        return None
    issue_title_str = str(issue_title) if issue_title else None
    return pattern, root_cause, fix, sample_events, repo, issue_title_str


def _run_in_thread(
    *,
    run_id: str,
    pattern: FailurePattern,
    root_cause: RootCause,
    fix: FixProposal,
    sample_events: list[LogEvent],
    issue_body: str | None,
    sink: EventSink,
    q: queue.Queue[Any],
) -> threading.Thread:
    settings = get_settings()
    router_ = build_router(settings)
    ctx = AgentContext(
        router=router_,
        code_host=build_code_host(settings),
        notifier=build_notifier(settings),
        event_sink=sink,
        run_id=run_id,
    )
    sandbox = build_sandbox(settings)
    project_root = Path(settings.verify_project_root) if settings.verify_project_root else None
    verifier = Verifier(
        ctx,
        sandbox,
        project_root=project_root,
        event_sink=sink,
        run_id=run_id,
        provision_deps=settings.verify_provision_deps,
    )

    def _exec() -> None:
        try:
            verifier.verify(pattern, root_cause, fix, sample_events, issue_body)
        except Exception as exc:
            log.exception("verify.failed", run_id=run_id)
            sink.emit(
                PipelineEvent(
                    type="error",
                    layer="output",
                    step="verify",
                    run_id=run_id,
                    payload={"error": type(exc).__name__, "message": str(exc)},
                )
            )
        finally:
            q.put(_END_SENTINEL)

    t = threading.Thread(target=_exec, daemon=True, name=f"tvastr-verify-{run_id}")
    t.start()
    return t


@router.post("/api/runs/{run_id}/verify")
async def verify_run(run_id: str, request: VerifyRequest) -> StreamingResponse:
    reconstructed = _reconstruct_from_run(run_id)
    if reconstructed is None:
        raise HTTPException(
            404,
            f"Run {run_id} not found or contains no fix to verify (no fix.generated event)",
        )
    pattern, root_cause, fix, sample_events, _repo, _issue_title = reconstructed

    q: queue.Queue[Any] = queue.Queue()
    file_sink = JsonlEventSink(run_path(run_id))
    queue_sink = _QueueEventSink(q)
    fanout = FanoutEventSink(queue_sink, file_sink)

    _run_in_thread(
        run_id=run_id,
        pattern=pattern,
        root_cause=root_cause,
        fix=fix,
        sample_events=sample_events,
        issue_body=request.issue_body,
        sink=fanout,
        q=q,
    )

    async def event_stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is _END_SENTINEL:
                yield "event: done\ndata: {}\n\n"
                return
            event: PipelineEvent = item
            yield f"event: {event.type}\ndata: {event.to_json()}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Tvastr-Run-Id": run_id, "Cache-Control": "no-cache"},
    )
