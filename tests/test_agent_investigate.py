"""Tests for the agentic investigator node."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent, _clamp_confidence, _parse_investigation
from tvastr.config import Settings
from tvastr.domain import FailurePattern, RoutingDecision, Sensitivity
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.llm.base import LLMResponse


def test_clamp_confidence():
    assert _clamp_confidence(0.5) == 0.5
    assert _clamp_confidence(2.0) == 1.0
    assert _clamp_confidence(-1.0) == 0.0
    assert _clamp_confidence("bad") == 0.0


def test_parse_investigation_actions_and_finish():
    a = _parse_investigation('{"thought":"t","actions":[{"search":"q"}]}')
    assert a["actions"] == [{"search": "q"}]
    f = _parse_investigation('{"root_cause":"rc","confidence":0.9,"done":true}')
    assert f["root_cause"] == "rc" and f["done"] is True
    assert _parse_investigation("not json") == {}


def test_parse_investigation_merges_multiple_objects():
    # The model sometimes emits a thought-with-empty-actions object THEN a
    # separate finish object holding the root_cause (real #17105 shape). Both
    # must survive the parse.
    text = (
        '{"thought":"analysis","actions":[]}\n'
        '{"root_cause":"rc here","suspected_files":["base.py"],"confidence":0.8,"done":true}'
    )
    merged = _parse_investigation(text)
    assert merged["root_cause"] == "rc here"
    assert merged["thought"] == "analysis"
    assert merged["confidence"] == 0.8


class _FakeHost:
    def __init__(self, search_map=None, files=None, dirs=None):
        self.search_map = search_map or {}
        self.files = files or {}
        self.dirs = dirs or {}
        self.reads: list[str] = []

    def search_code(self, query, *, limit=5):
        return list(self.search_map.get(query, []))[:limit]

    def get_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_dir(self, path):
        return list(self.dirs.get(path, []))

    def open_pull_request(self, draft):
        from tvastr.domain import PullRequestResult
        return PullRequestResult(
            pattern_id=draft.pattern_id, url="mock://pr/1",
            number=1, branch=draft.branch, created=True,
        )


class _SeqRouter:
    """Returns a scripted sequence of responses; falls back to empty string when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ""
        d = RoutingDecision(task=task.value, target="cloud", model="seq",
                            sensitivity=sensitivity, reason="seq")
        return LLMResponse(text=text, model="seq", target="cloud", mocked=True), d


def _agent(host, router, sink=None):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(router=router, code_host=host, notifier=build_notifier(settings),
                       event_sink=sink or ListEventSink(), run_id="t")
    return RemediationAgent(ctx)


def _pattern():
    return FailurePattern(fingerprint="f", title="WeaviateVectorStore broken filter",
                          representative_message="UnexpectedBehavior: broken filter")


def test_investigate_converges_with_actions_then_finish():
    host = _FakeHost(
        search_map={"weaviate filter": ["weaviate/base.py"]},
        files={
            "weaviate/base.py": "def query(): by_property('id')",
            "weaviate/utils.py": "uuid=id",
        },
        dirs={"weaviate": ["weaviate/base.py", "weaviate/utils.py"]},
    )
    router = _SeqRouter([
        (
            '{"thought":"look","actions":['
            '{"search":"weaviate filter"},'
            '{"read_file":"weaviate/base.py"},'
            '{"list_dir":"weaviate"},'
            '{"read_file":"weaviate/utils.py"}]}'
        ),
        (
            '{"root_cause":"query uses by_property(id) but store uses uuid; '
            'delete uses by_id","suspected_files":["weaviate/base.py"],'
            '"confidence":0.9,"done":true}'
        ),
    ])
    agent = _agent(host, router)
    out = agent._investigate(
        {"pattern": _pattern(), "sample_events": [], "issue_body": "no such prop 'id'"}
    )
    assert out["root_cause"].confidence == 0.9
    assert "weaviate/base.py" in out["code_files"]
    assert "weaviate/utils.py" in out["code_files"]
    assert out["root_cause"].suspected_files == ["weaviate/base.py"]


def test_investigate_captures_root_cause_in_second_object():
    """Regression for #17105: the model emitted a thought-with-empty-actions
    object followed by a finish object with the root_cause. The agent must
    capture it, not fall back to confidence 0.0."""
    host = _FakeHost(files={"base.py": "response.items()"})
    router = _SeqRouter([
        '{"thought":"ollama 0.4.x returns pydantic","actions":[]}\n'
        '{"root_cause":"base.py assumes dict responses; .items() on a pydantic '
        'GenerateResponse breaks","suspected_files":["base.py"],'
        '"confidence":0.8,"done":true}'
    ])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.8
    assert "assumes dict" in out["root_cause"].summary


def test_investigate_converges_on_root_cause_without_done():
    """A response carrying a root_cause (no explicit done, no actions) means the
    model converged — accept it rather than treating empty actions as give-up."""
    host = _FakeHost()
    router = _SeqRouter(['{"root_cause":"the real cause","confidence":0.7}'])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.7
    assert out["root_cause"].summary == "the real cause"


def test_investigate_hard_cap_returns_low_confidence():
    host = _FakeHost()
    # always returns actions, never done -> hit the cap -> one forced final
    # synthesis; that too yields actions (no root_cause) -> insufficient evidence
    router = _SeqRouter(['{"thought":"x","actions":[{"search":"q"}]}'] * 10)
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0
    assert router.calls == 5  # _MAX_INVESTIGATE_ROUNDS + forced final synthesis


def test_investigate_budget_exhausted_forces_final_synthesis():
    """Regression for #19293: the model read one file per round, said it had a
    strong lead, and the budget ran out one step short — the evidence was
    discarded for a hardcoded confidence-0.0 fallback. On exhaustion the agent
    must make ONE final tool-less call over the accumulated evidence and accept
    its root_cause."""
    host = _FakeHost(files={"a.py": "x", "b.py": "y", "c.py": "z", "d.py": "TOKEN_SOURCE"})
    router = _SeqRouter([
        '{"thought":"reading","actions":[{"read_file":"a.py"}]}',
        '{"thought":"reading","actions":[{"read_file":"b.py"}]}',
        '{"thought":"reading","actions":[{"read_file":"c.py"}]}',
        '{"thought":"strong lead","actions":[{"read_file":"d.py"}]}',
        '{"root_cause":"token counts dropped in d.py","suspected_files":["d.py"],'
        '"confidence":0.85}',
    ])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert router.calls == 5  # 4 rounds + forced synthesis
    assert out["root_cause"].confidence == 0.85
    assert out["root_cause"].summary == "token counts dropped in d.py"
    # round 4's read was fetched and must be visible to the final call
    assert "d.py" in out["code_files"]


def test_investigate_final_synthesis_sees_last_round_evidence():
    """The forced synthesis prompt must include files fetched in the LAST round
    (the #19293 shape: base.py was read in round 4 but never shown to the model)."""
    prompts = []

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
            prompts.append(prompt)
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    host = _FakeHost(files={"late.py": "LATE-EVIDENCE"})
    router = _CapRouter(
        ['{"thought":"x","actions":[{"search":"q"}]}'] * 3
        + ['{"thought":"x","actions":[{"read_file":"late.py"}]}',
           '{"root_cause":"rc","confidence":0.6}']
    )
    agent = _agent(host, router)
    agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert "LATE-EVIDENCE" in prompts[-1]
    assert "no more tools" in prompts[-1].lower() or "budget" in prompts[-1].lower()


def test_investigate_final_synthesis_failure_keeps_fallback():
    """If the forced synthesis call itself raises, degrade to the honest
    confidence-0.0 fallback (never crash)."""
    class _LastCallBoomRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
            if not self._responses:
                raise RuntimeError("llm down")
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _LastCallBoomRouter(['{"thought":"x","actions":[{"search":"q"}]}'] * 4)
    agent = _agent(_FakeHost(), router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0


def test_investigate_unparseable_finishes_low_confidence():
    host = _FakeHost()
    router = _SeqRouter(["not json at all"])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0


def test_investigate_prompt_includes_issue_body_and_method():
    captured = {}

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
            captured["prompt"] = prompt
            captured["system"] = system or ""
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter(['{"root_cause":"rc","confidence":0.8,"done":true}'])
    agent = _agent(_FakeHost(), router)
    agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "SENTINEL-BODY"})
    assert "SENTINEL-BODY" in captured["prompt"]
    sysl = captured["system"].lower()
    assert "root cause" in sysl
    assert "cross-reference" in sysl
    assert "hypothesis" in sysl
    assert "evidence" in sysl


def test_investigate_runs_on_act_path_in_graph():
    host = _FakeHost(files={"a.py": "x"})
    router = _SeqRouter([
        '{"root_cause":"rc","suspected_files":["a.py"],"confidence":0.9,"done":true}',
    ])
    sink = ListEventSink()
    agent = _agent(host, router, sink)
    agent.run({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    steps = [e.step for e in sink.events if e.type == "agent.node.start"]
    assert "investigate" in steps
    assert "reason_root_cause" not in steps and "expand_context" not in steps


def test_investigate_survives_tool_error():
    class _BoomSearchHost(_FakeHost):
        def search_code(self, query, *, limit=5):
            raise RuntimeError("network down")

    router = _SeqRouter([
        '{"thought":"x","actions":[{"search":"q"}]}',
        '{"root_cause":"rc","suspected_files":["a.py"],"confidence":0.8,"done":true}',
    ])
    agent = _agent(_BoomSearchHost(), router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].summary == "rc"  # tool error swallowed; loop continued to finish
