"""Wiring tests for issue-era code-host wrapping.

Verifies that when ``issue_era_retrieval=True``, the pipeline:
  - resolves the issue-era sha from the first sample event's timestamp,
  - wraps the agent's code host with ``IssueEraCodeHost``, and
  - emits a ``retrieval.issue_era`` event with a non-null sha.

And that with the flag off, none of that happens.
"""

from __future__ import annotations

from tvastr.agent import AgentContext, RemediationAgent
from tvastr.detection import FailureDetector, ThresholdEngine
from tvastr.events import ListEventSink
from tvastr.ingestion import SimulatedLogSource
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router
from tvastr.pipeline import RemediationPipeline
from tvastr.storage import build_audit_store


def _make_pipeline(
    settings, *, issue_era_retrieval: bool
) -> tuple[RemediationPipeline, ListEventSink]:
    """Build an isolated pipeline with a MockGitHubClient and the given flag value."""
    sink = ListEventSink()
    mock_host = MockGitHubClient()
    router = build_router(settings)
    router.event_sink = sink
    ctx = AgentContext(
        router=router,
        code_host=mock_host,
        notifier=build_notifier(settings),
        event_sink=sink,
        issue_era_retrieval=issue_era_retrieval,
    )
    pipeline = RemediationPipeline(
        detector=FailureDetector(),
        threshold=ThresholdEngine(
            recurrence_threshold=settings.recurrence_threshold,
            dedup_window_minutes=settings.dedup_window_minutes,
        ),
        agent=RemediationAgent(ctx),
        audit_store=build_audit_store(settings),
        log_source=SimulatedLogSource(),
        event_sink=sink,
    )
    return pipeline, sink


def test_pipeline_wraps_code_host_at_issue_era(settings, recurring_events):
    """Flag ON + resolvable sha → retrieval.issue_era event with ok=True and non-null sha."""
    pipeline, sink = _make_pipeline(settings, issue_era_retrieval=True)
    pipeline.run(events=recurring_events)

    era_events = [e for e in sink.events if e.type == "retrieval.issue_era"]
    assert era_events, "expected at least one retrieval.issue_era event when flag is on"
    payload = era_events[0].payload
    assert payload.get("sha"), "expected a non-null sha in the retrieval.issue_era payload"
    assert payload.get("ok") is True, "expected ok=True when sha resolved"


def test_pipeline_no_wrap_when_flag_off(settings, recurring_events):
    """Flag OFF → no retrieval.issue_era event emitted."""
    pipeline, sink = _make_pipeline(settings, issue_era_retrieval=False)
    pipeline.run(events=recurring_events)

    era_events = [e for e in sink.events if e.type == "retrieval.issue_era"]
    assert not era_events, "expected no retrieval.issue_era event when flag is off"


class _SpyHost(MockGitHubClient):
    """Records get_file_at_ref calls and pins a fixed issue-era sha."""

    def __init__(self) -> None:
        super().__init__()
        self.ref_reads: list[tuple[str, str]] = []

    def commit_before(self, iso_date: str) -> str:
        return "ERASHA1234"

    def get_file_at_ref(self, path: str, ref: str) -> str:
        self.ref_reads.append((path, ref))
        return f"# era {path}"


def test_issue_era_reads_seeded_traceback_file_at_sha(settings, recurring_events):
    """End-to-end: a traceback path in the issue body is extracted, the code host
    is wrapped, and the investigator reads that file AT the issue-era sha — proving
    both the wrap (reads served at sha) and the graph-seed (issue-body → suspected)."""
    pipeline, _ = _make_pipeline(settings, issue_era_retrieval=True)
    spy = _SpyHost()
    pipeline.agent.ctx.code_host = spy
    body = 'Traceback:\n  File "/x/llama_index/core/bug.py", line 1, in f\n    raise ValueError\n'

    pipeline.run(events=recurring_events, issue_body=body)

    assert ("llama-index-core/llama_index/core/bug.py", "ERASHA1234") in spy.ref_reads
    # M-2: the base host is restored after the run (no IssueEra-wrapping-IssueEra).
    assert pipeline.agent.ctx.code_host is spy
