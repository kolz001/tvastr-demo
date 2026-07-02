"""Tests for the compare_to_pr node in the remediation agent graph."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.analysis.fix_comparison import compare_fix_to_pr
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.config import Settings
from tvastr.domain import FailurePattern, FixProposal, RoutingDecision, Sensitivity
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import TaskType, build_router


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
    assert not any(e.type == "benchmark.skipped" for e in sink.events)


# --- verdict/equivalence normalization (the judge rarely speaks the enum) ---


class _OneShotRouter:
    def __init__(self, text):
        self.text = text

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        d = RoutingDecision(task=TaskType.FIX_COMPARISON.value, target="cloud",
                            model="s", sensitivity=sensitivity, reason="s")
        return LLMResponse(text=self.text, model="s", target="cloud", mocked=True), d


def _compare(text):
    fix = FixProposal(pattern_id="p", summary="s", changes=[], test_plan="")
    ref = PullRequestRef(30, "fix", "open", False, "u", 1)
    diff = PrDiff(files=[PrFile("x.py", "modified", 1, 0, "@@\n+x")])
    cmp_, _ = compare_fix_to_pr("t", "rc", fix, ref, diff, _OneShotRouter(text))
    return cmp_


def test_offschema_equivalent_normalizes_to_match():
    # Real #19906 shape: the judge said EQUIVALENT twice and was recorded divergent.
    c = _compare('{"verdict": "EQUIVALENT", "same_root_cause": true,'
                 ' "equivalence": "EQUIVALENT", "rationale": "r", "confidence": 0.9}')
    assert c.verdict == "match"
    assert c.equivalence == "functionally_equivalent"
    assert c.same_root_cause is True


def test_offschema_graded_normalizes_to_partial():
    c = _compare('{"verdict": "PARTIAL", "same_root_cause": true,'
                 ' "equivalence": "WEAK", "rationale": "r", "confidence": 0.7}')
    assert c.verdict == "partial"
    assert c.equivalence == "same_goal_different_approach"


def test_offschema_negative_stays_divergent():
    c = _compare('{"verdict": "DIFFERENT", "same_root_cause": false,'
                 ' "equivalence": "NOT_EQUIVALENT", "rationale": "r", "confidence": 0.8}')
    assert c.verdict == "divergent"
    assert c.equivalence == "addresses_different_cause"


def test_unknown_verdict_derived_from_equivalence():
    c = _compare('{"verdict": "banana", "same_root_cause": true,'
                 ' "equivalence": "functionally_equivalent", "rationale": "r", "confidence": 0.5}')
    assert c.verdict == "match"


def test_unknown_both_derived_from_same_root_cause():
    c = _compare('{"verdict": "banana", "same_root_cause": true,'
                 ' "equivalence": "banana", "rationale": "r", "confidence": 0.5}')
    assert c.verdict == "partial"
    assert c.equivalence == "same_goal_different_approach"
    c2 = _compare('{"verdict": "banana", "same_root_cause": false,'
                  ' "equivalence": "banana", "rationale": "r", "confidence": 0.5}')
    assert c2.verdict == "divergent"
    assert c2.equivalence == "addresses_different_cause"


def test_unparseable_response_keeps_divergent_fallback():
    c = _compare("no json at all")
    assert c.verdict == "divergent"
    assert c.confidence == 0.0
