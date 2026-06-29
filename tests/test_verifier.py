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
    FixRegister,
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
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    ProvisionResult,
    ReproducerKind,
    ReproducerSource,
    RunResult,
)
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
        self.provisioned: list[list[str]] = []
        self.provision_ok = True

    def write_file(self, relpath: str, content: str) -> None:
        self.written[relpath] = content

    def apply_changes(self, changes: list[FileChange]) -> None:
        self.applied.extend(changes)
        for change in changes:
            self.written[change.path] = change.patched_content

    def provision(self, dists: list[str]) -> ProvisionResult:
        self.provisioned.append(list(dists))
        ok = self.provision_ok
        return ProvisionResult(
            requested=list(dists),
            installed=list(dists) if ok else [],
            failed=[] if ok else list(dists),
            ok=ok,
        )

    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        self.run_history.append(cmd)
        if not self.responses:
            return RunResult(exit_code=0, stdout="", stderr="")
        return self.responses.pop(0)

    def discard(self) -> None:
        self.discarded = True


class _FakeSandbox:
    name = "fake"

    def __init__(
        self,
        responses: list[RunResult] | None = None,
        *,
        provision_ok: bool = True,
        prepares: list[list[RunResult]] | None = None,
    ) -> None:
        self._responses = responses or []
        self._provision_ok = provision_ok
        self._prepares = prepares  # per-attempt response lists; each prepare() pops the next
        self._prepares_idx = 0
        self.last_handle: _FakeHandle | None = None
        self.all_handles: list[_FakeHandle] = []  # every handle ever returned by prepare()

    def prepare(self) -> SandboxHandle:
        h = _FakeHandle()
        if self._prepares is not None:
            # per-attempt mode: each prepare() gets its own response list
            if self._prepares_idx < len(self._prepares):
                h.responses = list(self._prepares[self._prepares_idx])
                self._prepares_idx += 1
            else:
                h.responses = []
        else:
            h.responses = list(self._responses)
        h.provision_ok = self._provision_ok
        self.last_handle = h
        self.all_handles.append(h)
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
    verifier = Verifier(_ctx(), sandbox, repro_repair=False)  # single-cycle REPRO_BROKEN
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
    verifier = Verifier(_ctx(), sandbox, project_root=tmp_path, repro_repair=False)
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
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, repro_repair=False)
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


# ─── Provisioning wiring tests ────────────────────────────────────────────────


def _fix_with_changes(changes: list[FileChange]) -> FixProposal:
    return FixProposal(
        pattern_id="abc",
        summary="fix imports",
        changes=changes,
        test_plan="check it imports",
    )


def test_verify_provisions_derived_distributions() -> None:
    # Two files in the same dist + one in another → two deduped, sorted dists.
    changes = [
        FileChange(
            path=(
                "llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
                "llama_index/vector_stores/s3/base.py"
            ),
            patched_content="# fixed\n",
            rationale="r",
        ),
        FileChange(
            path=(
                "llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
                "llama_index/vector_stores/s3/utils.py"
            ),
            patched_content="# fixed2\n",
            rationale="r",
        ),
        FileChange(
            path=(
                "llama-index-integrations/vector_stores/llama-index-vector-stores-postgres/"
                "llama_index/vector_stores/postgres/base.py"
            ),
            patched_content="# fixed3\n",
            rationale="r",
        ),
    ]
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'foo'"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(), sandbox, provision_deps=True)
    verifier.verify(
        _pattern(), _root_cause(), _fix_with_changes(changes), [_event()], issue_body=None
    )
    assert sandbox.last_handle is not None
    assert sandbox.last_handle.provisioned == [
        ["llama-index-vector-stores-postgres", "llama-index-vector-stores-s3"]
    ]


def test_verify_does_not_provision_when_flag_off() -> None:
    changes = [
        FileChange(
            path=(
                "llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
                "llama_index/vector_stores/s3/base.py"
            ),
            patched_content="# fixed\n",
            rationale="r",
        ),
    ]
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'foo'"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(), sandbox, provision_deps=False)
    verifier.verify(
        _pattern(), _root_cause(), _fix_with_changes(changes), [_event()], issue_body=None
    )
    assert sandbox.last_handle is not None
    assert sandbox.last_handle.provisioned == []


def test_verify_emits_provision_event_and_proceeds_on_failure() -> None:
    changes = [
        FileChange(
            path=(
                "llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
                "llama_index/vector_stores/s3/base.py"
            ),
            patched_content="# fixed\n",
            rationale="r",
        ),
    ]
    sink = ListEventSink()
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: No module named 'foo'"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ],
        provision_ok=False,
    )
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, run_id="p1", provision_deps=True)
    verifier.verify(
        _pattern(), _root_cause(), _fix_with_changes(changes), [_event()], issue_body=None
    )
    types = [e.type for e in sink.events]
    assert "verify.provision" in types
    # verify still ran the baseline despite provision failure:
    assert "verify.baseline" in types


# ─── Source-overlay tests ─────────────────────────────────────────────────────

# A real path whose last non-hyphenated segment chain maps to an importable module.
_OVERLAY_PR_FILE = (
    "llama-index-integrations/vector_stores/llama-index-vector-stores-postgres/"
    "llama_index/vector_stores/postgres/base.py"
)


def test_overlay_on_no_repro_reproduces_then_verifies() -> None:
    """Behavioral repro: baseline prints BEHAVIOR_OK (no_repro); after the buggy
    overlay the retry does NOT print it (reproduces); rerun is OK → VERIFIED."""
    sandbox = _FakeSandbox(
        [
            # baseline #1: marker present → no_repro, triggers overlay
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
            # baseline #2 (retry, post-overlay): marker absent → reproduced
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            # rerun after fix: marker present → VERIFIED_VIA_BEHAVIOR
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, event_sink=sink, source_overlay=True)
    result = verifier.verify(
        _kerr_pattern(),
        _root_cause(),
        _fix(),
        [_event()],
        issue_body=None,
        pr_number=21447,
        pr_files=[_OVERLAY_PR_FILE],
    )
    types = [e.type for e in sink.events]
    assert "verify.overlay" in types
    # the post-overlay baseline re-emits verify.baseline with retry=True
    retry_baselines = [
        e for e in sink.events if e.type == "verify.baseline" and e.payload.get("retry")
    ]
    assert len(retry_baselines) == 1, "expected exactly one verify.baseline with retry=True"
    # apply_changes called twice: buggy overlay, then the agent fix
    assert sandbox.last_handle is not None
    assert len(sandbox.last_handle.applied) >= 2
    assert result.verdict in {Verdict.VERIFIED_VIA_BEHAVIOR, Verdict.VERIFIED_VIA_REPRODUCER}


def test_no_overlay_when_baseline_reproduces() -> None:
    """First baseline already reproduces → overlay path never taken."""
    sandbox = _FakeSandbox(
        [
            # baseline reproduces (no marker)
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            # rerun after fix
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, event_sink=sink, source_overlay=True)
    verifier.verify(
        _kerr_pattern(),
        _root_cause(),
        _fix(),
        [_event()],
        issue_body=None,
        pr_number=21447,
        pr_files=[_OVERLAY_PR_FILE],
    )
    assert "verify.overlay" not in [e.type for e in sink.events]


def test_no_overlay_without_pr_number() -> None:
    """no_repro baseline but pr_number=None → stays no_repro, no overlay."""
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, event_sink=sink, source_overlay=True)
    out = verifier.verify(
        _kerr_pattern(),
        _root_cause(),
        _fix(),
        [_event()],
        issue_body=None,
        pr_number=None,
        pr_files=None,
    )
    assert "verify.overlay" not in [e.type for e in sink.events]
    assert out.verdict == Verdict.NO_REPRO


def test_overlay_flag_off_skips_overlay() -> None:
    """source_overlay=False → overlay never runs even with pr_number set."""
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    sink = ListEventSink()
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, event_sink=sink, source_overlay=False)
    out = verifier.verify(
        _kerr_pattern(),
        _root_cause(),
        _fix(),
        [_event()],
        issue_body=None,
        pr_number=21447,
        pr_files=[_OVERLAY_PR_FILE],
    )
    assert "verify.overlay" not in [e.type for e in sink.events]
    assert out.verdict == Verdict.NO_REPRO


_OVERLAY_PR_FILE_B = (
    "llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
    "llama_index/vector_stores/s3/base.py"
)


class _ManifestHandle(_FakeHandle):
    """_FakeHandle variant that simulates the production Docker sandbox's
    manifest-replacement semantics: each ``apply_changes`` call REPLACES the
    previously-manifested entries in ``written``, just as a fresh container
    starts from the base image and only the current manifest's files are
    overlaid.  ``write_file`` entries (e.g. repro.py) are NOT cleared."""

    def __init__(self) -> None:
        super().__init__()
        self._manifest_paths: set[str] = set()

    def apply_changes(self, changes: list[FileChange]) -> None:
        # Evict files from the previous manifest (simulate fresh container)
        for p in self._manifest_paths:
            self.written.pop(p, None)
        self._manifest_paths = {c.path for c in changes}
        super().apply_changes(changes)


class _ManifestSandbox(_FakeSandbox):
    """_FakeSandbox variant that issues _ManifestHandle instances."""

    def prepare(self) -> SandboxHandle:
        h = _ManifestHandle()
        h.responses = list(self._responses)
        h.provision_ok = self._provision_ok
        self.last_handle = h
        self.all_handles.append(h)
        return h


def test_overlay_retains_human_only_files_in_rerun() -> None:
    """Buggy overlay for files the agent's fix does NOT touch must survive into the
    rerun sandbox.  Otherwise each fresh container reverts those files to the
    already-fixed upstream image — testing 'agent-fix + upstream-fix' and giving
    a false VERIFIED for an incomplete agent fix.

    pr_files = [A, B]: fix touches only A.  After verify, B must still hold the
    buggy overlay content (ref marker 'buggyparent' in it), and A must hold the
    agent's fix content (fix wins on overlapping path).

    Uses _ManifestSandbox/_ManifestHandle, which simulate production Docker
    manifest-replacement: each apply_changes call REPLACES the prior manifest,
    matching the behaviour of _DockerHandle.apply_changes (it overwrites
    manifest.json on every call)."""
    # Local fix that only touches A (_OVERLAY_PR_FILE), not B
    fix_a_only = FixProposal(
        pattern_id="abc",
        summary="fix only file A",
        changes=[
            FileChange(
                path=_OVERLAY_PR_FILE,
                patched_content="# agent-fixed-A-only\n",
                rationale="fix",
            )
        ],
        test_plan="check it",
    )
    sandbox = _ManifestSandbox(
        [
            # baseline #1: marker present → no_repro, triggers overlay
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
            # baseline #2 (retry, post-overlay): marker absent → reproduced
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            # rerun after fix: marker present → VERIFIED_VIA_BEHAVIOR
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox, source_overlay=True)
    verifier.verify(
        _kerr_pattern(),
        _root_cause(),
        fix_a_only,
        [_event()],
        issue_body=None,
        pr_number=21447,
        pr_files=[_OVERLAY_PR_FILE, _OVERLAY_PR_FILE_B],
    )
    assert sandbox.last_handle is not None
    handle = sandbox.last_handle
    # B must retain the buggy overlay content through the rerun (not revert to upstream)
    assert _OVERLAY_PR_FILE_B in handle.written, (
        "B must be in the rerun manifest — without the fix, apply_changes(fix.changes) "
        "replaces the manifest and B reverts to the upstream-fixed image"
    )
    assert "buggyparent" in handle.written[_OVERLAY_PR_FILE_B], (
        "B's content must carry the pre-fix (buggy) ref marker, not the upstream-fixed version"
    )
    # A must hold the agent's fix (fix wins over overlay on overlapping path)
    assert handle.written[_OVERLAY_PR_FILE] == "# agent-fixed-A-only\n", (
        "A must be overwritten by the agent's fix, not the overlay"
    )


# ─── Register-aware verifier tests ───────────────────────────────────────────


def _fix_reg(register: FixRegister) -> FixProposal:
    f = _fix()
    return f.model_copy(update={"register": register})


def test_document_register_short_circuits_unverified_doc_only() -> None:
    sandbox = _FakeSandbox([])  # must never be used
    verifier = Verifier(_ctx(), sandbox)
    out = verifier.verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.DOCUMENT), [_event()], issue_body=None
    )
    assert out.verdict == Verdict.UNVERIFIED_DOC_ONLY
    assert sandbox.last_handle is None  # never prepared a sandbox


def test_warn_register_greens_via_warning(monkeypatch: object) -> None:
    import tvastr.verification.verifier as vmod

    monkeypatch.setattr(  # type: ignore[attr-defined]
        vmod,
        "synthesize_reproducer",
        lambda *a, **k: vmod.Reproducer(
            source=ReproducerSource.CLAUDE,
            code="x",
            kind=ReproducerKind.BEHAVIORAL,
        ),
    )
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="AssertionError"),  # baseline: no warning
            RunResult(
                exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""
            ),  # rerun: warning fires
        ]
    )
    out = Verifier(_ctx(), sandbox).verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.WARN), [_event()], issue_body=None
    )
    assert out.verdict == Verdict.VERIFIED_VIA_WARNING
    assert out.oracle == "warning"


def test_better_error_register_greens_via_better_error(monkeypatch: object) -> None:
    import tvastr.verification.verifier as vmod

    monkeypatch.setattr(  # type: ignore[attr-defined]
        vmod,
        "synthesize_reproducer",
        lambda *a, **k: vmod.Reproducer(
            source=ReproducerSource.CLAUDE,
            code="x",
            kind=ReproducerKind.BEHAVIORAL,
        ),
    )
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="AssertionError"),
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    out = Verifier(_ctx(), sandbox).verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.BETTER_ERROR), [_event()], issue_body=None
    )
    assert out.verdict == Verdict.VERIFIED_VIA_BETTER_ERROR
    assert out.oracle == "better_error"


def test_fail_fast_register_greens_via_behavior(monkeypatch: object) -> None:
    """FAIL_FAST is not in _REGISTER_GREEN → falls back to the behavioral verdict
    (guards the zero-regression contract for the non-WARN/BETTER_ERROR registers)."""
    import tvastr.verification.verifier as vmod

    monkeypatch.setattr(  # type: ignore[attr-defined]
        vmod,
        "synthesize_reproducer",
        lambda *a, **k: vmod.Reproducer(
            source=ReproducerSource.CLAUDE,
            code="x",
            kind=ReproducerKind.BEHAVIORAL,
        ),
    )
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr=""),  # baseline: marker absent → reproduces
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),  # rerun: fixed
        ]
    )
    out = Verifier(_ctx(), sandbox).verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.FAIL_FAST), [_event()], issue_body=None
    )
    assert out.verdict == Verdict.VERIFIED_VIA_BEHAVIOR
    assert out.oracle == "behavior"


# ─── Repair-loop tests ────────────────────────────────────────────────────────


def test_repro_broken_triggers_repair_then_verifies() -> None:
    # Attempt 1 (fresh handle): baseline reproduces, rerun w/ different error → REPRO_BROKEN
    # → repair → Attempt 2 (fresh handle): baseline reproduces, rerun exit 0 → VERIFIED
    sandbox = _FakeSandbox(prepares=[
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),  # baseline
            RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),  # rerun → REPRO_BROKEN
        ],
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),  # baseline
            RunResult(exit_code=0, stdout="ok", stderr=""),  # rerun → VERIFIED
        ],
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=True)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.VERIFIED_VIA_REPRODUCER
    assert any(e.type == "verify.repro_repair" for e in sink.events)


def test_repro_repair_off_returns_repro_broken_once() -> None:
    # repro_repair=False → single fresh handle, REPRO_BROKEN returned immediately
    sandbox = _FakeSandbox(prepares=[
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
        ],
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=False)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.REPRO_BROKEN
    assert not any(e.type == "verify.repro_repair" for e in sink.events)


def test_repro_repair_budget_exhausted_returns_repro_broken() -> None:
    # 3 fresh handles, each REPRO_BROKEN → budget exhausted after _MAX_REPRO_REPAIR repairs
    _rb = RunResult(exit_code=1, stdout="", stderr="KeyError: 0")
    sandbox = _FakeSandbox(prepares=[
        [RunResult(exit_code=1, stdout="", stderr="x"), _rb],
        [RunResult(exit_code=1, stdout="", stderr="x"), _rb],
        [RunResult(exit_code=1, stdout="", stderr="x"), _rb],
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=True)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.REPRO_BROKEN
    repairs = [e for e in sink.events if e.type == "verify.repro_repair"]
    assert len(repairs) == 2  # _MAX_REPRO_REPAIR


def test_repair_uses_fresh_handle_each_attempt() -> None:
    """Regression: each repair attempt must prepare a FRESH sandbox handle.

    Without the fix, attempt 2 runs on the same handle as attempt 1, which
    already has the patch applied.  The bug makes attempt 2's baseline run
    against the patched (fixed) state → NO_REPRO instead of VERIFIED.
    """
    sandbox = _FakeSandbox(prepares=[
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
        ],
        [
            RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
            RunResult(exit_code=0, stdout="ok", stderr=""),
        ],
    ])
    verifier = Verifier(_ctx(), sandbox, repro_repair=True)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.VERIFIED_VIA_REPRODUCER
    # The critical assertion: sandbox.prepare() must have been called TWICE,
    # producing two distinct handle objects — one per attempt.
    assert len(sandbox.all_handles) == 2, (
        f"expected 2 distinct handles (one per attempt), got {len(sandbox.all_handles)}"
    )
    assert sandbox.all_handles[0] is not sandbox.all_handles[1], (
        "handles must be distinct objects — attempt 2 must not reuse attempt 1's handle"
    )
