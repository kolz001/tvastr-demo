import tvastr.verification.repro as repromod
from tvastr.config import Settings
from tvastr.domain import FixRegister
from tvastr.domain.models import FailurePattern, RootCause, Sensitivity
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification.models import BEHAVIOR_OK_MARKER
from tvastr.verification.repro import _build_prompt, synthesize_reproducer

# A minimal behavioral reproducer: kind=behavioral, ends with the marker.
_BEHAVIORAL_CODE = (
    f"# tvastr-kind: behavioral\nassert True\nprint('{BEHAVIOR_OK_MARKER}')\n"
)


class _AlwaysReturn:
    """LLM stub that always returns the same text regardless of task or prompt."""

    model = "stub"
    target = "cloud"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        return LLMResponse(text=self._text, model=self.model, target=self.target, mocked=True)


def _stub_router() -> object:
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = _AlwaysReturn(_BEHAVIORAL_CODE)
    return router


def _pat():
    return FailurePattern(fingerprint="f", title="oversized _node_content",
                          representative_message="metadata too large", exception_type=None,
                          count=1, sensitivity=Sensitivity.INTERNAL)


def _rc():
    return RootCause(pattern_id="f", summary="_node_content exceeds the filter limit",
                     suspected_files=["base.py"], confidence=0.8)


def test_warn_prompt_requests_warning_assertion():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.WARN)
    assert "catch_warnings" in p
    assert "warning" in p.lower()


def test_better_error_prompt_requests_error_assertion():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.BETTER_ERROR)
    assert "try" in p and "except" in p
    assert "error" in p.lower()


def test_repair_prompt_has_no_register_section():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.REPAIR)
    assert "catch_warnings" not in p


# ─── Critique-skip tests ──────────────────────────────────────────────────────
# The round-trip-biased critique must be SKIPPED for WARN/BETTER_ERROR registers
# (their oracles assert a warning/clear error, not a restored behavioral result).


def test_critique_not_called_for_warn_register(monkeypatch) -> None:
    calls: list = []

    def _recorder(*args, **kwargs):
        calls.append(args)
        return args[0]  # return code unchanged

    monkeypatch.setattr(repromod, "_critique_reproducer", _recorder)
    synthesize_reproducer(_pat(), _rc(), [], None, _stub_router(), register=FixRegister.WARN)
    assert calls == [], "critique must NOT be called for WARN register"


def test_critique_not_called_for_better_error_register(monkeypatch) -> None:
    calls: list = []

    def _recorder(*args, **kwargs):
        calls.append(args)
        return args[0]

    monkeypatch.setattr(repromod, "_critique_reproducer", _recorder)
    synthesize_reproducer(
        _pat(), _rc(), [], None, _stub_router(), register=FixRegister.BETTER_ERROR
    )
    assert calls == [], "critique must NOT be called for BETTER_ERROR register"


def test_critique_called_for_repair_register(monkeypatch) -> None:
    calls: list = []

    def _recorder(*args, **kwargs):
        calls.append(args)
        return args[0]

    monkeypatch.setattr(repromod, "_critique_reproducer", _recorder)
    synthesize_reproducer(_pat(), _rc(), [], None, _stub_router(), register=FixRegister.REPAIR)
    assert len(calls) == 1, "critique MUST be called for REPAIR register"
