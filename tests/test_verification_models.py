from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    ReproducerSource,
    Verdict,
    VerificationResult,
)


def test_new_verdicts_exist():
    assert Verdict.VERIFIED_VIA_BEHAVIOR.value == "verified_via_behavior"
    assert Verdict.MASKS_SYMPTOM.value == "masks_symptom"


def test_behavior_marker_constant():
    assert BEHAVIOR_OK_MARKER == "TVASTR_BEHAVIOR_OK"


def test_reproducer_kind_defaults_to_crash():
    r = Reproducer(source=ReproducerSource.CLAUDE, code="x")
    assert r.kind == ReproducerKind.CRASH


def test_reproducer_kind_can_be_behavioral():
    r = Reproducer(source=ReproducerSource.CLAUDE, code="x", kind=ReproducerKind.BEHAVIORAL)
    assert r.kind == ReproducerKind.BEHAVIORAL


def test_is_green_includes_behavior_excludes_masks():
    def _r(v):
        return VerificationResult(verdict=v, oracle="behavior", elapsed_s=0.0)
    assert _r(Verdict.VERIFIED_VIA_BEHAVIOR).is_green is True
    assert _r(Verdict.MASKS_SYMPTOM).is_green is False
    assert _r(Verdict.VERIFIED_VIA_REPRODUCER).is_green is True  # unchanged
    assert _r(Verdict.VERIFIED_VIA_SCOPED_TESTS).is_green is True
