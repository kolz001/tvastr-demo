"""POST /api/run — pick an issue, stream the pipeline live via SSE.

Also exposes ``GET /api/runs`` (list persisted runs) and
``GET /api/runs/{run_id}`` (replay the persisted event stream as JSON or SSE).

Each event is published to a fan-out sink: an in-process Queue feeds the SSE
generator for the live UI, and a JsonlEventSink writes ``data/runs/<run_id>.jsonl``
so the run is browsable later.
"""

from __future__ import annotations

import asyncio
import queue
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
    FanoutEventSink,
    JsonlEventSink,
    PipelineEvent,
    default_runs_dir,
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

_END_SENTINEL = object()


class RunRequest(BaseModel):
    repo: str = Field(default="run-llama/llama_index")
    issue_number: int
    dry_run: bool = True  # safe default — never opens a real PR from the UI without opt-in


class _QueueEventSink:
    """Adapts an in-process Queue to the EventSink protocol."""

    def __init__(self, q: queue.Queue[object]) -> None:
        self.q = q

    def emit(self, event: PipelineEvent) -> None:
        self.q.put(event)


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
    q: queue.Queue[object],
) -> threading.Thread:
    """Run the pipeline in a background thread; emit events onto ``q`` via ``sink``."""
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
            q.put(_END_SENTINEL)

    t = threading.Thread(target=_run, daemon=True, name=f"tvastr-run-{run_id}")
    t.start()
    return t


def _event_to_sse(event: PipelineEvent) -> str:
    return f"event: {event.type}\ndata: {event.to_json()}\n\n"


@router.post("/api/run")
async def run_pipeline(request: RunRequest) -> StreamingResponse:
    settings = get_settings()
    issue = _fetch_issue(
        request.repo,
        request.issue_number,
        use_mocks=settings.use_mocks,
        token=settings.github_token,
    )

    run_id = new_run_id()
    q: queue.Queue[object] = queue.Queue()
    file_sink = JsonlEventSink(run_path(run_id))
    queue_sink = _QueueEventSink(q)
    fanout = FanoutEventSink(queue_sink, file_sink)

    _start_pipeline_thread(
        request.repo,
        issue,
        dry_run=request.dry_run,
        sink=fanout,
        run_id=run_id,
        q=q,
    )

    async def event_stream() -> AsyncIterator[str]:
        # The pipeline's own pipeline.start (emitted via the sink) carries the
        # run_id in its payload, so the client gets it from the first streamed
        # event — no separate synthetic opener needed. This keeps the live
        # stream identical to the persisted/replayed one.
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is _END_SENTINEL:
                break
            assert isinstance(item, PipelineEvent)
            yield _event_to_sse(item)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind a proxy
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
