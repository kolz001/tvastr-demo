"""Tests for the ground_root_cause node."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.config import Settings
from tvastr.domain import FailurePattern, RootCause, RoutingDecision, Sensitivity
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse


def _pattern():
    return FailurePattern(fingerprint="f", title="t", representative_message="m")


def _root_cause():
    return RootCause(
        pattern_id="p", summary="original diagnosis", suspected_files=["a.py"],
        confidence=0.6, reasoning="original diagnosis",
    )


class _GroundingRouter:
    """Returns a scripted grounded answer (with sources) for DOC_GROUNDING."""

    def __init__(self, text="corrected diagnosis", sources=("https://docs/x",)):
        self.text = text
        self.sources = list(sources)
        self.tasks = []

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None, web_search=False):
        self.tasks.append((task, web_search))
        resp = LLMResponse(text=self.text, model="mock", target="cloud", sources=self.sources)
        decision = RoutingDecision(
            task=task.value, target="cloud", model="mock",
            sensitivity=sensitivity, reason="scripted",
        )
        return resp, decision


def _agent(sink, router, *, doc_grounding):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=router, code_host=MockGitHubClient(), notifier=build_notifier(settings),
        event_sink=sink, run_id="t", doc_grounding=doc_grounding,
    )
    return RemediationAgent(ctx)


def test_ground_skips_when_disabled():
    sink = ListEventSink()
    agent = _agent(sink, _GroundingRouter(), doc_grounding=False)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out == {}  # root_cause unchanged
    assert any(e.type == "doc.skipped" for e in sink.events)
    assert not any(e.type == "doc.grounded" for e in sink.events)


def test_ground_corrects_diagnosis_when_enabled():
    sink = ListEventSink()
    router = _GroundingRouter(text="Gemini 2.5 renamed the field to response_token_count")
    agent = _agent(sink, router, doc_grounding=True)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out["root_cause"].summary == "Gemini 2.5 renamed the field to response_token_count"
    assert out["doc_sources"] == ["https://docs/x"]
    assert router.tasks[0][1] is True  # web_search=True was passed
    grounded = [e for e in sink.events if e.type == "doc.grounded"]
    assert grounded and grounded[-1].payload["searched"] is True
    assert grounded[-1].payload["sources"] == ["https://docs/x"]


def test_ground_degrades_when_call_raises():
    class _BoomRouter:
        def run(self, *a, **k):
            raise RuntimeError("api down")

    sink = ListEventSink()
    agent = _agent(sink, _BoomRouter(), doc_grounding=True)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out == {}  # root_cause unchanged
    assert any(e.type == "doc.skipped" for e in sink.events)


def test_act_path_routes_through_ground_root_cause():
    # The compiled graph must wire gate 'act' -> ground_root_cause -> generate_fix.
    sink = ListEventSink()
    agent = _agent(sink, _GroundingRouter(), doc_grounding=True)
    # The mock code host + scripted router drive a full act-path run.
    agent.run({"pattern": _pattern(), "sample_events": []})
    steps = [e.step for e in sink.events if e.type == "agent.node.start"]
    assert "ground_root_cause" in steps


def test_ground_skips_when_root_cause_missing():
    sink = ListEventSink()
    agent = _agent(sink, _GroundingRouter(), doc_grounding=True)
    out = agent._ground_root_cause({"pattern": _pattern()})  # no root_cause in state
    assert out == {}
    assert any(e.type == "doc.skipped" for e in sink.events)
    assert not any(e.type == "doc.grounded" for e in sink.events)
