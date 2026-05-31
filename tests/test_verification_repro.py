"""Reproducer synthesis: extract-from-body and Claude-synth paths."""

from __future__ import annotations

from dataclasses import dataclass

from tvastr.agent.context import AgentContext
from tvastr.config import Settings
from tvastr.domain import FailurePattern, LogEvent, RootCause, Sensitivity, Severity
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification import ReproducerSource, synthesize_reproducer
from tvastr.verification.repro import extract_from_body

# ─── extract_from_body ────────────────────────────────────────────────────


def test_extract_pulls_first_runnable_python_block() -> None:
    body = (
        "Hi, this is broken:\n"
        "```python\n"
        "from llama_index.llms.openai import OpenAI\n"
        "OpenAI()\n"
        "```\n"
        "Trace was attached."
    )
    assert extract_from_body(body) is not None
    assert "from llama_index" in extract_from_body(body)


def test_extract_handles_unmarked_fence() -> None:
    body = "Repro:\n```\nimport llama_index\nllama_index.foo()\n```\n"
    assert extract_from_body(body) is not None


def test_extract_skips_traceback_blocks() -> None:
    body = (
        "```\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 1, in <module>\n'
        "    OpenAI()\n"
        "ValueError: nope\n"
        "```"
    )
    assert extract_from_body(body) is None


def test_extract_skips_syntactically_invalid_blocks() -> None:
    body = "```python\nthis is not python code at all !!!\n```"
    assert extract_from_body(body) is None


def test_extract_skips_blocks_without_imports_or_actions() -> None:
    # A literal docstring with no import/call/assign looks too thin to be a real repro.
    body = '```python\n"""just a docstring"""\n```'
    assert extract_from_body(body) is None


def test_extract_returns_none_for_empty_body() -> None:
    assert extract_from_body("") is None


# ─── synthesize_reproducer ────────────────────────────────────────────────


@dataclass
class _ScriptedLLM:
    response_text: str
    model: str = "claude-opus-4-7"
    target: str = "cloud"

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        return LLMResponse(
            text=self.response_text, model=self.model, target=self.target, mocked=True
        )


def _ctx_with_cloud_llm(llm) -> AgentContext:
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = llm
    return AgentContext(
        router=router, code_host=MockGitHubClient(), notifier=build_notifier(settings)
    )


def _pattern() -> FailurePattern:
    return FailurePattern(
        fingerprint="abc",
        title="ModuleNotFoundError in app",
        representative_message="ModuleNotFoundError: No module named 'llama_index.llms.openai'",
        exception_type="ModuleNotFoundError",
        count=3,
        sensitivity=Sensitivity.INTERNAL,
    )


def _root_cause() -> RootCause:
    return RootCause(
        pattern_id="abc",
        summary="post-v0.10 split moved the OpenAI integration into its own package",
        suspected_files=["app/rag.py"],
        confidence=0.8,
    )


def test_synthesizer_prefers_issue_body_when_runnable() -> None:
    body = "```python\nfrom llama_index.llms.openai import OpenAI\nOpenAI()\n```"
    ctx = _ctx_with_cloud_llm(_ScriptedLLM("this should NOT be used"))
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], body, ctx.router)
    assert repro.source == ReproducerSource.ISSUE_BODY
    assert "from llama_index" in repro.code
    assert "should NOT" not in repro.code


def test_synthesizer_falls_back_to_claude_without_runnable_block() -> None:
    llm = _ScriptedLLM("from llama_index.llms.openai import OpenAI\nOpenAI()\n")
    ctx = _ctx_with_cloud_llm(llm)
    repro = synthesize_reproducer(
        _pattern(),
        _root_cause(),
        [LogEvent(service="svc", severity=Severity.ERROR, message="boom")],
        issue_body="(no code block)",
        router=ctx.router,
    )
    assert repro.source == ReproducerSource.CLAUDE
    assert "from llama_index" in repro.code
    assert repro.expected_exception == "ModuleNotFoundError"


def test_synthesizer_strips_markdown_fences_from_claude_output() -> None:
    llm = _ScriptedLLM("```python\nfrom llama_index.llms.openai import OpenAI\n```")
    ctx = _ctx_with_cloud_llm(llm)
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, ctx.router)
    assert repro.code.startswith("from llama_index")
    assert "```" not in repro.code
