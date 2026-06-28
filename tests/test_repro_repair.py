from tvastr.domain import FailurePattern, RootCause, Sensitivity
from tvastr.llm.base import LLMResponse
from tvastr.verification.models import Reproducer, ReproducerKind, ReproducerSource
from tvastr.verification.repro import repair_reproducer


class _Router:
    def __init__(self, text, *, boom=False):
        self.text = text
        self.boom = boom
        self.last_prompt = None
        self.last_system = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        if self.boom:
            raise RuntimeError("llm down")
        self.last_prompt = prompt
        self.last_system = system
        return LLMResponse(text=self.text, model="m", target="cloud", mocked=True), None


def _pat():
    return FailurePattern(fingerprint="f", title="t", representative_message="boom",
                          exception_type="AttributeError", sensitivity=Sensitivity.INTERNAL)


def _rc():
    return RootCause(pattern_id="f", summary="s", suspected_files=["base.py"], confidence=0.8)


def _orig():
    return Reproducer(source=ReproducerSource.CLAUDE, code="# tvastr-kind: crash\nold()\n",
                      expected_exception="AttributeError", kind=ReproducerKind.CRASH)


def test_repair_returns_new_reproducer_and_passes_error():
    repaired_code = (
        "# tvastr-kind: crash\nfrom ollama import GenerateResponse\nGenerateResponse()\n"
    )
    router = _Router(repaired_code)
    out = repair_reproducer(_orig(), {"rerun_stderr_tail": "KeyError: 0 in /work/repro.py"},
                            _pat(), _rc(), router)
    assert "GenerateResponse" in out.code
    assert "KeyError: 0" in router.last_prompt          # real error fed back
    assert "real" in (router.last_system or "").lower()  # "use real installed objects" guidance


def test_repair_returns_original_on_error():
    orig = _orig()
    out = repair_reproducer(orig, {"rerun_stderr_tail": "x"}, _pat(), _rc(), _Router("", boom=True))
    assert out is orig
