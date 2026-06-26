from tvastr.domain import FixRegister
from tvastr.domain.models import FailurePattern, RootCause, Sensitivity
from tvastr.verification.repro import _build_prompt


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
