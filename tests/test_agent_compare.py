"""Tests for the compare_to_pr node in the remediation agent graph."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def _agent(sink):
    # Mirrors tests/test_agent_investigate.py::_ctx, plus an event_sink so we
    # can assert on emitted benchmark events.
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=build_router(settings),
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
        event_sink=sink,
        run_id="t",
    )
    return RemediationAgent(ctx)


def test_compare_emits_skipped_without_pr_ref():
    sink = ListEventSink()
    agent = _agent(sink)
    pattern = FailurePattern(
        fingerprint="f",
        title="ModuleNotFoundError in x",
        representative_message="ModuleNotFoundError: no mod",
    )
    agent.run({"pattern": pattern, "sample_events": []})  # no pr_ref in state
    assert any(e.type == "benchmark.skipped" for e in sink.events)
    assert not any(e.type == "benchmark.compared" for e in sink.events)


def test_compare_emits_compared_with_pr_ref():
    sink = ListEventSink()
    agent = _agent(sink)
    pattern = FailurePattern(
        fingerprint="f",
        title="ModuleNotFoundError in x",
        representative_message="ModuleNotFoundError: no mod",
    )
    ref = PullRequestRef(30, "fix", "open", False, "u", 1)
    diff = PrDiff(files=[PrFile("x.py", "modified", 1, 0, "@@\n+x")])
    agent.run({"pattern": pattern, "sample_events": [], "pr_ref": ref, "pr_diff": diff})
    assert any(e.type == "benchmark.compared" for e in sink.events)
