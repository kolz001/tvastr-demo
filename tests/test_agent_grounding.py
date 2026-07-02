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
    """Returns a scripted grounded answer (with sources) for DOC_GROUNDING.

    When called for the investigator (ROOT_CAUSE with _INVESTIGATE_SYSTEM), returns
    a valid done-JSON so the confidence gate passes and ground_root_cause is reached.
    """

    def __init__(self, text="corrected diagnosis", sources=("https://docs/x",)):
        self.text = text
        self.sources = list(sources)
        self.tasks = []

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None, web_search=False):
        self.tasks.append((task, web_search))
        # Investigator calls need a finish JSON so confidence gate routes to "act".
        if system and '"done": true' in system:
            text = '{"root_cause": "mock root cause", "confidence": 0.9, "done": true}'
            sources: list[str] = []
        else:
            text = self.text
            sources = self.sources
        resp = LLMResponse(text=text, model="mock", target="cloud", sources=sources)
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


# --- SDK-schema grounding wiring ---


class _SeqRouter:
    """Returns a scripted sequence of responses; falls back to empty string when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None,
            web_search=False):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ""
        d = RoutingDecision(task=task.value, target="cloud", model="seq",
                            sensitivity=sensitivity, reason="seq")
        return LLMResponse(text=text, model="seq", target="cloud", mocked=True), d


def _agent_with_grounding(router, *, sdk_schema, sink=None):
    agent = _agent(sink or ListEventSink(), router, doc_grounding=True)
    agent.ctx.sdk_schema_grounding = sdk_schema
    return agent


_PROBE_YES = (
    '{"relevant": true, "package": "google-genai", "version_hint": null,'
    ' "keywords": ["usage_metadata"]}'
)


def _grounding_state():
    rc = RootCause(pattern_id="p", summary="tokens not mapped",
                   suspected_files=["utils.py"], confidence=0.6)
    return {"pattern": _pattern(), "root_cause": rc,
            "code_context": "(ctx)", "issue_body": "body"}


def _snippet_dir(tmp_path, name="google-genai@latest"):
    pkg = tmp_path / name
    (pkg / "google").mkdir(parents=True)
    (pkg / "google" / "types.py").write_text(
        "class UsageMetadata:\n    usage_metadata: int\n"
        "    response_token_count: int\n"
    )
    return pkg


def test_grounding_prompt_enriched_with_sdk_schema(tmp_path, monkeypatch):
    prompts = []

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            prompts.append((task.value, prompt))
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter([_PROBE_YES, "corrected diagnosis text"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk", lambda pkg, ver: _snippet_dir(tmp_path)
    )
    agent = _agent_with_grounding(router, sdk_schema=True)
    out = agent._ground_root_cause(_grounding_state())
    tasks = [t for t, _ in prompts]
    assert tasks == ["schema_probe", "doc_grounding"]
    grounding_prompt = prompts[1][1]
    assert "response_token_count" in grounding_prompt
    assert "ground truth" in grounding_prompt.lower()
    assert out["root_cause"].summary == "corrected diagnosis text"


def test_probe_not_relevant_leaves_prompt_unchanged(monkeypatch):
    prompts = []

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            prompts.append((task.value, prompt))
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter(['{"relevant": false}', "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    agent = _agent_with_grounding(router, sdk_schema=True)
    agent._ground_root_cause(_grounding_state())
    grounding_prompt = prompts[1][1]
    assert "ground truth" not in grounding_prompt.lower()


def test_fetch_failure_degrades_and_emits_skip_event(monkeypatch):
    sink = ListEventSink()
    router = _SeqRouter([_PROBE_YES, "grounded"])
    monkeypatch.setattr("tvastr.agent.graph.fetch_sdk", lambda *a, **k: None)
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    out = agent._ground_root_cause(_grounding_state())
    assert out["root_cause"].summary == "grounded"  # grounding still ran
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    assert ev[0].payload["ok"] is False
    assert "fetch" in ev[0].payload["reason"]


def test_sdk_schema_success_emits_event(tmp_path, monkeypatch):
    sink = ListEventSink()
    router = _SeqRouter([_PROBE_YES, "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk", lambda pkg, ver: _snippet_dir(tmp_path)
    )
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    agent._ground_root_cause(_grounding_state())
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    p = ev[0].payload
    assert p["ok"] is True and p["package"] == "google-genai" and p["snippets"] == 1
    assert p["version"] == "latest"


def test_sdk_schema_version_reflects_pin_fallback_not_requested_pin(tmp_path, monkeypatch):
    # I1/M2 wiring: a pin that fell back must report the dir that actually
    # got installed ("latest"), never the version_hint that failed to
    # resolve — fetch_sdk itself returns the "...@latest" dir on fallback.
    probe_pinned = (
        '{"relevant": true, "package": "google-genai", "version_hint": "9.9.9",'
        ' "keywords": ["usage_metadata"]}'
    )
    sink = ListEventSink()
    router = _SeqRouter([probe_pinned, "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk",
        lambda pkg, ver: _snippet_dir(tmp_path, name="google-genai@latest"),
    )
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    agent._ground_root_cause(_grounding_state())
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    assert ev[0].payload["version"] == "latest"  # not the failed "9.9.9" pin


def test_sdk_schema_evidence_exception_degrades_and_grounding_still_returns(monkeypatch):
    # M3: fetch/extract/format must never raise out of _ground_root_cause —
    # the grounding LLM call still runs and doc.sdk_schema reports ok:false.
    sink = ListEventSink()
    router = _SeqRouter([_PROBE_YES, "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    out = agent._ground_root_cause(_grounding_state())
    assert out["root_cause"].summary == "grounded"
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    assert ev[0].payload["ok"] is False
    assert "schema evidence error" in ev[0].payload["reason"]


def test_flag_off_makes_zero_probe_calls():
    router = _SeqRouter(["grounded only"])
    agent = _agent_with_grounding(router, sdk_schema=False)
    agent._ground_root_cause(_grounding_state())
    assert router.calls == 1  # doc_grounding only


def test_probe_exception_degrades(monkeypatch):
    class _BoomFirstRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            if task.value == "schema_probe":
                raise RuntimeError("llm down")
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _BoomFirstRouter(["grounded"])
    agent = _agent_with_grounding(router, sdk_schema=True)
    out = agent._ground_root_cause(_grounding_state())
    assert out["root_cause"].summary == "grounded"
