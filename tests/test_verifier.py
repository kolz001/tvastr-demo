"""End-to-end verifier flow with scripted sandbox + scripted Claude.

Asserts on the verdict and the event sequence — the public contract the UI
relies on. Real Docker is exercised by smoke tests only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tvastr.agent.context import AgentContext
from tvastr.config import Settings
from tvastr.domain import (
    FailurePattern,
    FileChange,
    FixProposal,
    LogEvent,
    RootCause,
    Sensitivity,
    Severity,
)
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification import Verdict, Verifier
from tvastr.verification.models import RunResult
from tvastr.verification.sandbox import SandboxHandle

# ─── Test doubles ─────────────────────────────────────────────────────────


class _FakeHandle:
    def __init__(self) -> None:
        self.root = Path("/tmp/fake")
        self.written: dict[str, str] = {}
        self.applied: list[FileChange] = []
        self.run_history: list[list[str]] = []
        self.responses: list[RunResult] = []
        self.discarded = False

    def write_file(self, relpath: str, content: str) -> None:
        self.written[relpath] = content

    def apply_changes(self, changes: list[FileChange]) -> None:
        self.applied.extend(changes)
        for change in changes:
            self.written[change.path] = change.patched_content

    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        self.run_history.append(cmd)
        if not self.responses:
            return RunResult(exit_code=0, stdout="", stderr="")
        return self.responses.pop(0)

    def discard(self) -> None:
        self.discarded = True


class _FakeSandbox:
    name = "fake"

    def __init__(self, responses: list[RunResult]) -> None:
        self._responses = responses
        self.last_handle: _FakeHandle | None = None

    def prepare(self) -> SandboxHandle:
        h = _FakeHandle()
        h.responses = list(self._responses)
        self.last_handle = h
        return h


@dataclass
class _ScriptedLLM:
    response_text: str
    model: str = "claude-opus-4-7"
    target: str = "cloud"

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        return LLMResponse(
            text=self.response_text, model=self.model, target=self.target, mocked=True
        )


# ─── Fixtures ─────────────────────────────────────────────────────────────


def _ctx(scripted_response: str = "import nothing\n") -> AgentContext:
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = _ScriptedLLM(scripted_response)
    return AgentContext(
        router=router,
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
    )


def _pattern() -> FailurePattern:
    return FailurePattern(
        fingerprint="abc",
        title="ModuleNotFoundError in app",
        representative_message="ModuleNotFoundError: No module named 'foo'",
        exception_type="ModuleNotFoundError",
        count=3,
        sensitivity=Sensitivity.INTERNAL,
    )


def _root_cause() -> RootCause:
    return RootCause(
        pattern_id="abc", summary="missing package", suspected_files=["app.py"], confidence=0.8
    )


def _fix() -> FixProposal:
    return FixProposal(
        pattern_id="abc",
        summary="fix import path",
        changes=[FileChange(path="app.py", patched_content="# fixed\n", rationale="r")],
        test_plan="check it imports",
    )


def _event() -> LogEvent:
    return LogEvent(service="svc", severity=Severity.ERROR, message="ModuleNotFoundError")


# ─── Verdicts ─────────────────────────────────────────────────────────────


def test_verdict_verified_when_baseline_fails_and_rerun_passes() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'foo'"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, run_id="t1")
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)

    assert result.verdict == Verdict.VERIFIED_VIA_REPRODUCER
    assert result.oracle == "reproducer"
    assert result.is_green
    assert sandbox.last_handle and sandbox.last_handle.discarded
    # The fix's patched files were applied to the sandbox.
    assert "app.py" in sandbox.last_handle.written


def test_verdict_still_broken_when_rerun_still_raises() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
        ]
    )
    verifier = Verifier(_ctx(), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.STILL_BROKEN
    assert not result.is_green


def test_verdict_no_repro_when_baseline_already_passes() -> None:
    sandbox = _FakeSandbox([RunResult(exit_code=0, stdout="ok", stderr="")])
    verifier = Verifier(_ctx(), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.NO_REPRO
    assert "did not reproduce" in result.evidence["reason"]


def test_event_sequence_published_to_sink() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, run_id="run-7")
    verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)

    types = [e.type for e in sink.events]
    assert types == [
        "verify.start",
        "verify.repro_synth",
        "verify.baseline",
        "verify.patch_applied",
        "verify.rerun",
        "verify.result",
    ]
    assert all(e.run_id == "run-7" for e in sink.events)
    result_event = sink.events[-1]
    assert result_event.payload["verdict"] == Verdict.VERIFIED_VIA_REPRODUCER.value


def test_uses_issue_body_repro_when_provided() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(scripted_response="THIS_SHOULD_NOT_BE_USED"), sandbox, event_sink=sink)
    body = "```python\nimport foo\nfoo.run()\n```"
    verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=body)

    repro_event = next(e for e in sink.events if e.type == "verify.repro_synth")
    assert repro_event.payload["source"] == "issue_body"
    # The sandbox actually got the issue-body code, not the scripted Claude output.
    assert sandbox.last_handle is not None
    assert "import foo" in sandbox.last_handle.written["repro.py"]
    assert "THIS_SHOULD_NOT_BE_USED" not in sandbox.last_handle.written["repro.py"]
