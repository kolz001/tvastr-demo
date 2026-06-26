from dataclasses import dataclass

from tvastr.config import Settings
from tvastr.domain import FailurePattern, RootCause, RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification.models import BEHAVIOR_OK_MARKER, ReproducerKind, ReproducerSource
from tvastr.verification.repro import (
    _build_prompt,
    _critique_reproducer,
    _parse_kind,
    synthesize_reproducer,
)


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


# Task 2: Self-critique gate tests
_WEAK = (
    f"# tvastr-kind: behavioral\nnode = mk_empty()\nassert node_get() == []\n"
    f"print('{BEHAVIOR_OK_MARKER}')"
)
_STRONG = (
    f"# tvastr-kind: behavioral\nput(msgs)\nassert get() == msgs\n"
    f"print('{BEHAVIOR_OK_MARKER}')"
)
_CRASH = "# tvastr-kind: crash\nraise KeyError('sub_dicts')"


class _SeqRouter:
    """Router stub returning a scripted sequence of responses, recording tasks."""

    def __init__(self, responses, raise_on=None):
        self._responses = list(responses)
        self.tasks = []
        self._raise_on = raise_on  # a TaskType to raise on, or None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.tasks.append(task)
        if self._raise_on is not None and task == self._raise_on:
            raise RuntimeError("critique boom")
        text = self._responses.pop(0)
        decision = RoutingDecision(
            task=task.value, target="cloud", model="seq", sensitivity=sensitivity, reason="seq"
        )
        return LLMResponse(text=text, model="seq", target="cloud", mocked=True), decision


def test_critique_returns_strengthened(_=None):
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_STRONG])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _STRONG
    assert r.tasks == [TaskType.REPRO_CRITIQUE]


def test_critique_graceful_on_error():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([], raise_on=TaskType.REPRO_CRITIQUE)
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK  # original kept on failure


def test_critique_keeps_original_when_rewrite_empty():
    r = _SeqRouter(["   \n"])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK


def test_critique_keeps_original_when_behavioral_rewrite_drops_marker():
    r = _SeqRouter(["# tvastr-kind: behavioral\nassert get() == msgs\n"])  # no marker
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK


def test_critique_allows_explicit_crash_downgrade():
    r = _SeqRouter([_CRASH])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _CRASH  # honored — no marker required for a crash repro


def test_synthesize_runs_critique_on_behavioral():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_WEAK, _STRONG])  # synth -> weak; critique -> strong
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, r)
    assert repro.code == _STRONG
    assert repro.kind == ReproducerKind.BEHAVIORAL
    assert r.tasks == [TaskType.FIX_GENERATION, TaskType.REPRO_CRITIQUE]


def test_synthesize_skips_critique_on_crash():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_CRASH])  # only the synth response — critique must not be called
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, r)
    assert repro.kind == ReproducerKind.CRASH
    assert r.tasks == [TaskType.FIX_GENERATION]  # critique skipped
