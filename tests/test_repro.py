from dataclasses import dataclass

from tvastr.config import Settings
from tvastr.domain import FailurePattern, RootCause, Sensitivity
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification.models import BEHAVIOR_OK_MARKER, ReproducerKind, ReproducerSource
from tvastr.verification.repro import _build_prompt, _parse_kind, synthesize_reproducer


@dataclass
class _ScriptedLLM:
    response_text: str
    model: str = "claude-opus-4-7"
    target: str = "cloud"

    def complete(self, prompt, *, system=None):
        return LLMResponse(
            text=self.response_text,
            model=self.model,
            target=self.target,
            mocked=True,
        )


def _ctx_router(text):
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = _ScriptedLLM(text)
    return router


def _pattern():
    return FailurePattern(
        fingerprint="abc", title="KeyError in VectorMemory",
        representative_message="KeyError: 'sub_dicts'", exception_type="KeyError",
        count=3, sensitivity=Sensitivity.INTERNAL,
    )


def _root_cause():
    return RootCause(
        pattern_id="abc",
        summary="missing key",
        suspected_files=["vm.py"],
        confidence=0.8,
    )


def test_parse_kind_behavioral():
    assert _parse_kind("# tvastr-kind: behavioral\nimport x\n") == ReproducerKind.BEHAVIORAL


def test_parse_kind_crash():
    assert _parse_kind("# tvastr-kind: crash\nimport x\n") == ReproducerKind.CRASH


def test_parse_kind_defaults_crash_when_absent():
    assert _parse_kind("import x\nprint(1)\n") == ReproducerKind.CRASH


def test_synthesize_sets_behavioral_kind():
    code = f"# tvastr-kind: behavioral\nassert True\nprint('{BEHAVIOR_OK_MARKER}')\n"
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, _ctx_router(code))
    assert repro.kind == ReproducerKind.BEHAVIORAL
    assert repro.source == ReproducerSource.CLAUDE


def test_synthesize_defaults_crash_kind():
    code = "raise KeyError\n"
    repro = synthesize_reproducer(
        _pattern(), _root_cause(), [], None, _ctx_router(code)
    )
    assert repro.kind == ReproducerKind.CRASH


def test_issue_body_fastpath_is_crash_kind():
    body = "Repro:\n```python\nimport foo\nfoo.bar()\n```\n"
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], body, _ctx_router("unused"))
    assert repro.source == ReproducerSource.ISSUE_BODY
    assert repro.kind == ReproducerKind.CRASH


def test_prompt_mentions_behavioral_marker_and_fallback():
    prompt = _build_prompt(_pattern(), _root_cause(), [])
    assert BEHAVIOR_OK_MARKER in prompt
    assert "tvastr-kind" in prompt


def test_parse_kind_rejects_behavioral_substring_in_crash_tag():
    # "behavioral" appears, but the tag VALUE is crash -> must be CRASH.
    code = "# tvastr-kind: crash (was behavioral)\nraise KeyError\n"
    assert _parse_kind(code) == ReproducerKind.CRASH


def test_parse_kind_handles_empty_code():
    assert _parse_kind("") == ReproducerKind.CRASH
    assert _parse_kind("   \n  \n") == ReproducerKind.CRASH


def test_parse_kind_tolerates_extra_spacing():
    assert _parse_kind("#   tvastr-kind:   behavioral\nx\n") == ReproducerKind.BEHAVIORAL


def test_repro_critique_is_a_cloud_task():
    from tvastr.llm.router import _LOCAL_TASKS, TaskType
    assert TaskType.REPRO_CRITIQUE.value == "repro_critique"
    assert TaskType.REPRO_CRITIQUE not in _LOCAL_TASKS


def test_system_prompt_forbids_asserting_degraded_state():
    from tvastr.verification.repro import _SYSTEM
    s = _SYSTEM
    assert "round-trip" in s
    assert "FORBIDDEN" in s
    assert "degraded" in s
    assert "SUPPRESSES" in s
