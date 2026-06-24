"""Tests for reasoning-emitted retrieval directives."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent, _parse_reasoning
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def test_parse_reasoning_extracts_directives():
    text = (
        'Some preamble. {"root_cause": "field renamed", "need_more_context": true, '
        '"next_targets": {"queries": ["candidates_token_count"], "paths": ["a/b.py"]}}'
    )
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == "field renamed"
    assert need_more is True
    assert targets == {"queries": ["candidates_token_count"], "paths": ["a/b.py"]}


def test_parse_reasoning_falls_back_on_prose():
    text = "The root cause is a contract mismatch between components."
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == text
    assert need_more is False
    assert targets == {"queries": [], "paths": []}


def test_parse_reasoning_handles_partial_json():
    # JSON present but missing the root_cause key -> treat as prose/fallback.
    text = '{"need_more_context": true, "next_targets": {"queries": ["x"]}}'
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == text
    assert need_more is False
    assert targets == {"queries": [], "paths": []}


def _ctx(sink, router):
    settings = Settings(use_mocks=True, audit_backend="memory")
    return AgentContext(
        router=router, code_host=MockGitHubClient(),
        notifier=build_notifier(settings), event_sink=sink, run_id="t",
    )


def _pattern():
    return FailurePattern(
        fingerprint="f", title="No token count for Gemini 2.5",
        representative_message="UnexpectedBehavior: No token count for Gemini 2.5",
    )


def test_reason_node_writes_directives(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = scripted_reasoning(
        settings,
        ['{"root_cause": "parser ignores renamed field", "need_more_context": true, '
         '"next_targets": {"queries": ["candidates_token_count"], "paths": []}}'],
    )
    agent = RemediationAgent(_ctx(ListEventSink(), router))
    out = agent._reason_root_cause({"pattern": _pattern(), "suspected_files": [], "code_context": ""})
    assert out["need_more_context"] is True
    assert out["next_targets"]["queries"] == ["candidates_token_count"]
    assert out["root_cause"].summary == "parser ignores renamed field"


def test_reason_node_prose_response_is_single_pass():
    # The default MockClaudeClient returns prose -> no directives -> need_more False.
    settings = Settings(use_mocks=True, audit_backend="memory")
    agent = RemediationAgent(_ctx(ListEventSink(), build_router(settings)))
    out = agent._reason_root_cause({"pattern": _pattern(), "suspected_files": [], "code_context": ""})
    assert out["need_more_context"] is False
    assert out["next_targets"] == {"queries": [], "paths": []}
