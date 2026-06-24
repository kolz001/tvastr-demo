"""Tests for the expand_context node (iterative retrieval)."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient


class _FakeHost:
    """Code host with scripted search + file contents, for deterministic retrieval."""

    def __init__(self, search_map=None, files=None):
        self.search_map = search_map or {}      # query -> [paths]
        self.files = files or {}                 # path -> content
        self.fetched: list[str] = []

    def search_code(self, query, *, limit=5):
        return list(self.search_map.get(query, []))[:limit]

    def get_file(self, path):
        self.fetched.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def open_pull_request(self, draft):  # unused here
        raise NotImplementedError


def _agent(host, sink=None):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=None, code_host=host, notifier=build_notifier(settings),
        event_sink=sink or ListEventSink(), run_id="t",
    )
    return RemediationAgent(ctx)


def _pattern():
    return FailurePattern(fingerprint="f", title="t", representative_message="m")


def test_expand_executes_queries_and_paths_and_merges():
    host = _FakeHost(
        search_map={"candidates_token_count": ["core/token_counting.py"]},
        files={"core/token_counting.py": "code A", "llms/utils.py": "code B"},
    )
    agent = _agent(host)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": ["candidates_token_count"], "paths": ["llms/utils.py"]},
        "code_files": {}, "retrieved_paths": set(), "retrieval_iterations": 0,
    })
    assert out["code_files"] == {"core/token_counting.py": "code A", "llms/utils.py": "code B"}
    assert out["retrieval_iterations"] == 1
    assert "core/token_counting.py" in out["code_context"]


def test_expand_dedups_already_retrieved():
    host = _FakeHost(files={"a.py": "x"})
    agent = _agent(host)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": ["a.py"]},
        "code_files": {"a.py": "x"}, "retrieved_paths": {"a.py"}, "retrieval_iterations": 0,
    })
    assert host.fetched == []           # already had it; nothing re-fetched
    assert out["code_files"] == {"a.py": "x"}


def test_expand_caps_total_files():
    files = {f"f{i}.py": "c" for i in range(20)}
    host = _FakeHost(files=files)
    agent = _agent(host)
    existing = {f"e{i}.py": "c" for i in range(10)}  # already 10 in context
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": list(files)},
        "code_files": dict(existing), "retrieved_paths": set(existing), "retrieval_iterations": 0,
    })
    assert len(out["code_files"]) == 12   # _MAX_CONTEXT_FILES


def test_expand_records_missing_path_and_survives():
    host = _FakeHost(files={})            # every get_file raises
    sink = ListEventSink()
    agent = _agent(host, sink)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": ["ghost.py"]},
        "code_files": {}, "retrieved_paths": set(), "retrieval_iterations": 0,
    })
    assert out["code_files"] == {}        # no crash; nothing added
    ends = [e for e in sink.events if e.type == "agent.node.end" and e.step == "expand_context"]
    assert ends and ends[-1].payload["missing_paths"] == ["ghost.py"]


def _loop_agent(host, router, sink):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=router, code_host=host, notifier=build_notifier(settings),
        event_sink=sink, run_id="t",
    )
    return RemediationAgent(ctx)


def _directive(need_more, queries=(), paths=()):
    import json
    return json.dumps({
        "root_cause": "analysis", "need_more_context": need_more,
        "next_targets": {"queries": list(queries), "paths": list(paths)},
    })


def test_loop_converges_then_proceeds(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = _FakeHost(
        search_map={"candidates_token_count": ["core/token_counting.py"]},
        files={"core/token_counting.py": "real parser code"},
    )
    # Pass 0 asks for more; pass 1 is satisfied.
    router = scripted_reasoning(settings, [
        _directive(True, queries=["candidates_token_count"]),
        _directive(False),
    ])
    sink = ListEventSink()
    agent = _loop_agent(host, router, sink)
    pattern = FailurePattern(
        fingerprint="f", title="No token count",
        representative_message="UnexpectedBehavior: No token count",
    )
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [
        e for e in sink.events
        if e.type == "agent.node.start" and e.step == "expand_context"
    ]
    assert len(expands) == 1                       # exactly one extra round
    assert "core/token_counting.py" in host.fetched  # it reached the real file


def test_loop_stops_at_hard_cap(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = _FakeHost(files={f"f{i}.py": "c" for i in range(10)})
    # A NEW path each round (never the no-new-targets guard) — only the hard cap stops it.
    router = scripted_reasoning(settings, [
        _directive(True, paths=["f0.py"]),
        _directive(True, paths=["f1.py"]),
        _directive(True, paths=["f2.py"]),
        _directive(True, paths=["f3.py"]),
    ])
    sink = ListEventSink()
    agent = _loop_agent(host, router, sink)
    pattern = FailurePattern(fingerprint="f", title="t", representative_message="m")
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [
        e for e in sink.events if e.type == "agent.node.start" and e.step == "expand_context"
    ]
    assert len(expands) == 2  # _MAX_EXPANSIONS — fresh targets each round, so the CAP stops it


def test_loop_stops_when_no_new_targets(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = _FakeHost(files={"a.py": "c"})
    # Round 1 fetches a.py; round 2 re-requests only a.py (already seen) -> stop.
    router = scripted_reasoning(settings, [
        _directive(True, paths=["a.py"]),
        _directive(True, paths=["a.py"]),
    ])
    sink = ListEventSink()
    agent = _loop_agent(host, router, sink)
    pattern = FailurePattern(fingerprint="f", title="t", representative_message="m")
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [
        e for e in sink.events if e.type == "agent.node.start" and e.step == "expand_context"
    ]
    assert len(expands) == 1  # second round blocked by the no-new-targets guard


def test_loop_back_compat_single_pass_on_prose():
    # Real mock router returns prose -> need_more False -> no expansion at all.
    from tvastr.llm.router import build_router
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = MockGitHubClient()
    sink = ListEventSink()
    agent = _loop_agent(host, build_router(settings), sink)
    pattern = FailurePattern(
        fingerprint="f", title="ModuleNotFoundError in x",
        representative_message="ModuleNotFoundError: no mod",
    )
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [e for e in sink.events if e.step == "expand_context"]
    assert expands == []
