"""POST /api/run — pick an issue, start the pipeline as a background job.

The POST responds ``202 {"run_id": ...}`` immediately; it never waits on the
pipeline. The pipeline runs in a daemon thread that appends events to
``data/runs/<run_id>.jsonl`` via a ``JsonlEventSink`` — the JSONL file is the
single source of truth for a run's history.

``GET /api/runs/{run_id}/stream`` attaches to that run: it replays whatever is
already on disk, then (if the run's thread is still alive) tails the file for
new lines, polling until a terminal event or thread death. One endpoint
serves live attach, mid-run re-attach after a client drop, and pure replay of
a finished run — the client can't tell the difference and doesn't need to.

Also exposes ``GET /api/runs`` (list persisted runs) and
``GET /api/runs/{run_id}`` (fetch the persisted event stream as JSON or SSE).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from tvastr.config import get_settings
from tvastr.events import (
    EventSink,
    JsonlEventSink,
    PipelineEvent,
    _parse_event_line,
    default_runs_dir,
    is_terminal_event,
    list_runs,
    load_events,
    new_run_id,
    run_path,
)
from tvastr.ingestion import SimulatedLogSource
from tvastr.ingestion.github_issues import (
    GitHubIssuesFetcher,
    IssueRecord,
    MockGitHubIssuesFetcher,
    issue_to_events,
)
from tvastr.logging import get_logger
from tvastr.pipeline import build_pipeline

log = get_logger(__name__)

router = APIRouter(tags=["run"])

# Live pipeline threads by run_id. Entries remove themselves when the thread
# finishes, so "in the dict and alive" ⇔ the run is still producing events.
_IN_FLIGHT: dict[str, threading.Thread] = {}


class RunRequest(BaseModel):
    repo: str = Field(default="run-llama/llama_index")
    issue_number: int
    dry_run: bool = True  # safe default — never opens a real PR from the UI without opt-in


def _fetch_issue(
    repo: str, issue_number: int, *, use_mocks: bool, token: str | None
) -> IssueRecord:
    fetcher: GitHubIssuesFetcher | MockGitHubIssuesFetcher
    if use_mocks or not token:
        fetcher = MockGitHubIssuesFetcher(repo)
    else:
        fetcher = GitHubIssuesFetcher(repo, token=token)
    # The fetchers return paged lists; for "fetch this one issue" we just scan
    # the first page. For real GitHub this is one API call by number; we can
    # avoid the scan by hitting the issue endpoint directly, but for v1 keeping
    # the fetcher interface uniform is simpler.
    if isinstance(fetcher, MockGitHubIssuesFetcher):
        for rec in fetcher.fetch(limit=100):
            if rec.number == issue_number:
                return rec
        raise HTTPException(404, f"Mock issue #{issue_number} not found in {repo}")

    # Live: hit the issue endpoint directly via PyGithub.
    from github import Github

    gh = Github(token) if token else Github()
    issue = gh.get_repo(repo).get_issue(issue_number)
    reactions = getattr(issue, "reactions", None) or {}
    return IssueRecord(
        number=issue.number,
        title=issue.title or "",
        body=issue.body or "",
        created_at=issue.created_at,
        url=issue.html_url,
        labels=[lbl.name for lbl in issue.labels],
        reactions=int(reactions.get("total_count", 0)) if isinstance(reactions, dict) else 0,
        thumbs_up=int(reactions.get("+1", 0)) if isinstance(reactions, dict) else 0,
        comments=int(getattr(issue, "comments", 0) or 0),
    )


def _start_pipeline_thread(
    repo: str,
    issue: IssueRecord,
    *,
    dry_run: bool,
    sink: EventSink,
    run_id: str,
) -> threading.Thread:
    """Run the pipeline in a background thread, persisting events via ``sink``."""
    settings = get_settings().model_copy(update={"dry_run": dry_run} if dry_run else {})

    def _run() -> None:
        try:
            events = issue_to_events(issue, default_service=repo.split("/")[-1])
            if not events:
                # Surface as a single event then close the stream.
                sink.emit(
                    PipelineEvent(
                        type="error",
                        layer="ingestion",
                        step="issue_to_events",
                        run_id=run_id,
                        payload={
                            "reason": "no error signature found in issue title or body",
                            "issue_number": issue.number,
                            "issue_title": issue.title,
                        },
                    )
                )
                return
            from tvastr.analysis.pr_discovery import discover_pr, fetch_pr_diff

            pr_ref = discover_pr(
                repo, issue.number, token=settings.github_token, use_mocks=settings.use_mocks
            )
            pr_diff = None
            if pr_ref is not None:
                try:
                    pr_diff = fetch_pr_diff(repo, pr_ref.number, token=settings.github_token)
                except Exception as exc:
                    log.warning("run.pr_diff_failed", error=str(exc))
                    pr_ref = None
            # Single-issue mode: bypass the recurrence threshold (the user has
            # explicitly picked this issue; the threshold is for autonomous mode).
            settings_for_run = settings.model_copy(update={"recurrence_threshold": 1})
            # Synthesize a "log source" that returns nothing; we'll pass events directly.
            pipeline = build_pipeline(
                settings_for_run,
                log_source=SimulatedLogSource(),  # unused; events passed below
                event_sink=sink,
                run_id=run_id,
            )
            # Override the threshold engine to require only 1 occurrence.
            pipeline.threshold.recurrence_threshold = 1
            pipeline.run(
                events=events,
                run_meta={
                    "run_id": run_id,
                    "repo": repo,
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "issue_url": issue.url,
                    "dry_run": dry_run,
                    "mode": "mock" if settings.use_mocks else "live",
                },
                pr_ref=pr_ref,
                pr_diff=pr_diff,
                issue_body=issue.body or "",
            )
        except Exception as exc:
            log.exception("run.failed", run_id=run_id)
            sink.emit(
                PipelineEvent(
                    type="error",
                    layer="output",
                    step="pipeline",
                    run_id=run_id,
                    payload={"error": type(exc).__name__, "message": str(exc)},
                )
            )
        finally:
            _IN_FLIGHT.pop(run_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"tvastr-run-{run_id}")
    _IN_FLIGHT[run_id] = t
    t.start()
    return t


def _event_to_sse(event: PipelineEvent) -> str:
    return f"event: {event.type}\ndata: {event.to_json()}\n\n"


@router.post("/api/run", status_code=202)
async def run_pipeline(request: RunRequest) -> JSONResponse:
    """Start a pipeline run and return its id immediately.

    The run executes in a background thread writing data/runs/<id>.jsonl;
    attach to it (live or after the fact) via GET /api/runs/{id}/stream.
    """
    settings = get_settings()
    issue = _fetch_issue(
        request.repo,
        request.issue_number,
        use_mocks=settings.use_mocks,
        token=settings.github_token,
    )
    run_id = new_run_id()
    _start_pipeline_thread(
        request.repo,
        issue,
        dry_run=request.dry_run,
        sink=JsonlEventSink(run_path(run_id)),
        run_id=run_id,
    )
    return JSONResponse({"run_id": run_id}, status_code=202)


_TAIL_POLL_S = 0.25


@router.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Replay a run's events, then tail while its thread is alive.

    One endpoint for live attach, mid-run re-attach, and replay: reads the
    persisted JSONL (single source of truth) instead of coupling to the
    producing request.
    """
    path = run_path(run_id)
    if not path.exists():
        raise HTTPException(404, f"run {run_id!r} not found")

    async def event_stream() -> AsyncIterator[str]:
        offset = 0
        saw_terminal = False
        while True:
            # Snapshot liveness BEFORE reading, not after. If we read first
            # and check after, a writer that emits its last event(s) and
            # deregisters in that gap causes us to break having already read
            # a stale (pre-final-write) copy of the file, silently dropping
            # those events. Checking first means: when we observe "not
            # alive", the thread's `finally` (which pops it from
            # `_IN_FLIGHT`) has already run, which only happens after every
            # event is written — so the read that follows is guaranteed to
            # see everything, and it's safe to stop after this pass.
            thread = _IN_FLIGHT.get(run_id)
            alive = thread is not None and thread.is_alive()
            text = path.read_text(encoding="utf-8")
            chunk = text[offset:]
            # Only advance past whole lines. A trailing fragment with no
            # newline yet is a line still being written; consuming it now
            # (old behavior) would drop it, since the eventual write of the
            # rest of the line plus the newline would land past our offset
            # as two unparseable halves. Leave it for the next poll.
            last_newline = chunk.rfind("\n")
            complete, trailing = (
                (chunk[: last_newline + 1], chunk[last_newline + 1 :])
                if last_newline != -1
                else ("", chunk)
            )
            offset += len(complete)
            for line in complete.splitlines():
                if not line.strip():
                    continue
                event = _parse_event_line(line)
                if event is None:
                    log.warning("run.stream.bad_line", run_id=run_id)
                    continue
                yield _event_to_sse(event)
                if is_terminal_event(event):
                    saw_terminal = True
            if saw_terminal or not alive:
                # Final drain: the writer is done and will never complete a
                # dangling trailing fragment. Log once (not a per-poll spam)
                # and move on rather than looping forever on it.
                if trailing.strip():
                    log.warning("run.stream.torn_trailing_line", run_id=run_id)
                break
            await asyncio.sleep(_TAIL_POLL_S)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Tvastr-Run-Id": run_id,
        },
    )


@router.get("/api/runs")
def list_persisted_runs(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    runs = list_runs(default_runs_dir())[:limit]
    return JSONResponse([asdict(r) for r in runs])


@router.get("/api/runs/{run_id}", response_model=None)
def get_persisted_run(
    run_id: str, format: Literal["json", "sse"] = Query(default="json")
) -> StreamingResponse | JSONResponse:
    path = run_path(run_id)
    if not path.exists():
        raise HTTPException(404, f"run {run_id!r} not found")
    events = list(load_events(path))
    if format == "json":
        return JSONResponse([asdict(e) for e in events])

    async def replay() -> AsyncIterator[str]:
        for event in events:
            yield _event_to_sse(event)

    return StreamingResponse(replay(), media_type="text/event-stream")
