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
from tvastr.verification.models import BEHAVIOR_OK_MARKER, RunResult
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


def test_verdict_repro_broken_when_rerun_raises_different_exception() -> None:
    """Real case from run-llama/llama_index: rerun fails with TypeError(unexpected
    keyword argument) instead of the original. The reproducer is wrong, not the
    fix — surface it as REPRO_BROKEN so the UI re-arms the verify button."""
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(
                exit_code=1,
                stdout="",
                stderr=(
                    "TypeError: MutableMappingKVStore.__init__() got an "
                    "unexpected keyword argument 'mutable_mapping_factory'"
                ),
            ),
        ]
    )
    verifier = Verifier(_ctx(), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.REPRO_BROKEN
    assert not result.is_green
    assert result.oracle == "none"  # the reproducer is what's untrustworthy
    assert "different exception" in result.evidence["hint"]


def test_repro_broken_skips_scoped_tests(monkeypatch, tmp_path: Path) -> None:
    """A broken rerun gives no fix signal — don't spend a scoped-test run on it
    (and never let a scoped failure masquerade as REGRESSION)."""
    import tvastr.verification.verifier as vmod

    monkeypatch.setattr(vmod, "discover_scoped_tests", lambda changes, root: ["tests/test_x.py"])
    scoped_calls: list[list[str]] = []

    def _run_scoped(handle, scoped):
        scoped_calls.append(scoped)
        return RunResult(exit_code=1, stdout="", stderr=""), {"failed": 1}

    monkeypatch.setattr(vmod, "run_scoped_tests", _run_scoped)

    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=1, stdout="", stderr="TypeError: nope"),
        ]
    )
    verifier = Verifier(_ctx(), sandbox, project_root=tmp_path)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.REPRO_BROKEN
    assert scoped_calls == []


def test_verdict_environmental_error_on_rerun_timeout() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=-1, stdout="", stderr="", timed_out=True),
        ]
    )
    verifier = Verifier(_ctx(), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.ENVIRONMENTAL_ERROR


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


# ─── Behavioral reproducer tests ──────────────────────────────────────────────

_BEHAVIORAL_REPRO = (
    f"# tvastr-kind: behavioral\nassert get() == put_value\nprint('{BEHAVIOR_OK_MARKER}')\n"
)


def _kerr_pattern() -> FailurePattern:
    """FailurePattern with exception_type='KeyError' for behavioral tests."""
    return FailurePattern(
        fingerprint="abc",
        title="KeyError in app",
        representative_message="KeyError: 'sub_dicts'",
        exception_type="KeyError",
        count=3,
        sensitivity=Sensitivity.INTERNAL,
    )


def test_behavioral_verified_when_marker_present_post_patch() -> None:
    sandbox = _FakeSandbox(
        [
            # baseline: bug reproduces (KeyError, no marker)
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            # rerun after real fix: marker printed, exit 0
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_kerr_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.VERIFIED_VIA_BEHAVIOR
    assert result.oracle == "behavior"
    assert result.is_green


def test_behavioral_masks_symptom_when_assertion_fails_post_patch() -> None:
    # Regression for #21896: masking fix stops the crash but the assertion fails.
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="Traceback...\nAssertionError"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_kerr_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.MASKS_SYMPTOM
    assert not result.is_green
    assert result.oracle == "behavior"


def test_behavioral_still_broken_when_original_exception_remains() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_kerr_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.STILL_BROKEN


def test_behavioral_repro_broken_on_other_exception() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="TypeError: unexpected kwarg"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_kerr_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.REPRO_BROKEN


def test_behavioral_no_repro_when_baseline_prints_marker() -> None:
    sandbox = _FakeSandbox(
        [
            # baseline already passes — marker present, no reproduction
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_kerr_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.NO_REPRO
