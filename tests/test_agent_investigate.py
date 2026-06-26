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


def test_investigate_hard_cap_returns_low_confidence():
    host = _FakeHost()
    # always returns actions, never done -> hit the cap -> insufficient evidence
    router = _SeqRouter(['{"thought":"x","actions":[{"search":"q"}]}'] * 10)
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0
    assert router.calls == 4  # _MAX_INVESTIGATE_ROUNDS


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
