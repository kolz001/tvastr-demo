"""Tests for the expand_context node (iterative retrieval)."""

from __future__ import annotations

import pytest

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
