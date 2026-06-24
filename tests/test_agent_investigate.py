"""Investigation fallback + evidence-based confidence.

Non-crashing bug reports (synthetic ``UnexpectedBehavior: <title>`` events,
no exception type, no stack trace) must still be investigable — the agent
searches the codebase with the behavior description, and the confidence gate
sees an honest score for that weaker evidence.
"""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.config import Settings
from tvastr.domain import FailurePattern, LogEvent, Sensitivity, Severity
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def _ctx() -> AgentContext:
    settings = Settings(use_mocks=True, audit_backend="memory")
    return AgentContext(
        router=build_router(settings),
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
    )


def _non_crashing_pattern() -> FailurePattern:
    return FailurePattern(
        fingerprint="nc1",
        title="UnexpectedBehavior: No token count for Gemini 2.5 in llama_index",
        representative_message="UnexpectedBehavior: No token count for Gemini 2.5",
        exception_type=None,
        count=1,
        sensitivity=Sensitivity.INTERNAL,
    )


def _event(message: str, stack: str | None = None) -> LogEvent:
    return LogEvent(
        service="llama_index", severity=Severity.WARNING, message=message, stack_trace=stack
    )


def test_non_crashing_pattern_is_investigated_via_search() -> None:
    agent = RemediationAgent(_ctx())
    state = agent.run(
        {
            "pattern": _non_crashing_pattern(),
            "sample_events": [_event("UnexpectedBehavior: No token count for Gemini 2.5")],
        }
    )
    # The search fallback found candidate files, so the agent acted instead of
    # unconditionally escalating (the pre-fix behavior: confidence stuck at 0.3).
    assert state["suspected_files"]
    assert state["evidence_source"] == "search"
    assert state["root_cause"].confidence == 0.6
    assert state["outcome"] in {"pr_opened", "dry_run"}


def test_stack_trace_evidence_keeps_full_confidence() -> None:
    pattern = FailurePattern(
        fingerprint="c1",
        title="ModuleNotFoundError in llama_index",
        representative_message="ModuleNotFoundError: No module named 'foo'",
        exception_type="ModuleNotFoundError",
        count=3,
        sensitivity=Sensitivity.INTERNAL,
    )
    trace = 'Traceback (most recent call last):\n  File "app/rag.py", line 3, in <module>\n'
    agent = RemediationAgent(_ctx())
    state = agent.run(
        {"pattern": pattern, "sample_events": [_event("ModuleNotFoundError: foo", trace)]}
    )
    assert state["evidence_source"] == "stack_trace"
    assert state["root_cause"].confidence == 0.8
