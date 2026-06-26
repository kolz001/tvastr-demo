from tvastr.domain import FixProposal, FixRegister
from tvastr.verification.models import Verdict, VerificationResult


def test_fix_register_values():
    assert FixRegister.REPAIR == "repair"
    assert {r.value for r in FixRegister} == {
        "repair", "fail_fast", "warn", "better_error", "document"
    }


def test_fix_proposal_defaults_to_repair():
    fp = FixProposal(pattern_id="p", summary="s")
    assert fp.register == FixRegister.REPAIR


def test_fix_proposal_accepts_register():
    fp = FixProposal(pattern_id="p", summary="s", register=FixRegister.WARN)
    assert fp.register == FixRegister.WARN


def test_new_verdicts_exist_and_greenness():
    assert Verdict.VERIFIED_VIA_WARNING == "verified_via_warning"
    assert Verdict.VERIFIED_VIA_BETTER_ERROR == "verified_via_better_error"
    assert Verdict.UNVERIFIED_DOC_ONLY == "unverified_doc_only"
    green = VerificationResult(
        verdict=Verdict.VERIFIED_VIA_WARNING, oracle="warning", elapsed_s=1.0
    )
    assert green.is_green
    assert VerificationResult(
        verdict=Verdict.VERIFIED_VIA_BETTER_ERROR, oracle="better_error", elapsed_s=1.0
    ).is_green
    assert not VerificationResult(
        verdict=Verdict.UNVERIFIED_DOC_ONLY, oracle="none", elapsed_s=1.0
    ).is_green
