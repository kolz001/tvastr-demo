"""Grade tvastr's generated fix against the maintainer's PR (ground truth).

File overlap is computed deterministically in code; the LLM judges root-cause
agreement, functional equivalence, the overall verdict, and a rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from tvastr.analysis._jsonutil import extract_json
from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.analysis.prompts import FIX_COMPARISON_SYSTEM
from tvastr.domain import FixProposal, RoutingDecision, Sensitivity
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SCHEMA_HINT = (
    'Return JSON: {"verdict": "match"|"partial"|"divergent", '
    '"same_root_cause": true|false, '
    '"equivalence": "functionally_equivalent"|"same_goal_different_approach"|'
    '"addresses_different_cause", "rationale": "2-4 sentences", '
    '"confidence": 0.0-1.0}. Use EXACTLY these lowercase enum values — '
    "values like EQUIVALENT, WEAK, or DIFFERENT are invalid."
)
_VERDICTS = {"match", "partial", "divergent"}
_EQUIV = {
    "functionally_equivalent",
    "same_goal_different_approach",
    "addresses_different_cause",
}

# The judge model frequently answers in its own vocabulary (EQUIVALENT, WEAK,
# DIFFERENT, ...) despite the schema hint. 39/44 persisted comparison calls
# were off-enum; the old code silently coerced ALL of them to the worst bucket
# — grading judged-EQUIVALENT fixes as divergent. Normalize by meaning instead.
_VERDICT_ALIASES = {
    "match": "match", "equivalent": "match", "same": "match",
    "functionally_equivalent": "match",
    "partial": "partial", "weak": "partial", "weaker": "partial",
    "weak_equivalent": "partial", "partial_match": "partial",
    "divergent": "divergent", "different": "divergent",
    "not_equivalent": "divergent", "none": "divergent", "reject": "divergent",
    "incorrect": "divergent", "different_behavior": "divergent",
}
_EQUIV_ALIASES = {
    "functionally_equivalent": "functionally_equivalent",
    "equivalent": "functionally_equivalent", "match": "functionally_equivalent",
    "same_goal_different_approach": "same_goal_different_approach",
    "partial": "same_goal_different_approach", "weak": "same_goal_different_approach",
    "weaker": "same_goal_different_approach",
    "weak_equivalent": "same_goal_different_approach",
    "addresses_different_cause": "addresses_different_cause",
    "different": "addresses_different_cause",
    "not_equivalent": "addresses_different_cause", "none": "addresses_different_cause",
    "reject": "addresses_different_cause", "incorrect": "addresses_different_cause",
    "different_behavior": "addresses_different_cause",
}
_EQUIV_TO_VERDICT = {
    "functionally_equivalent": "match",
    "same_goal_different_approach": "partial",
    "addresses_different_cause": "divergent",
}


def _normalize(raw_verdict: str, raw_equiv: str, same_root_cause: bool) -> tuple[str, str]:
    """Map the judge's free vocabulary onto the schema enums by meaning.

    Unknown equivalence falls back to what same_root_cause implies; unknown
    verdict falls back to what the (normalized) equivalence implies.
    """
    equiv = _EQUIV_ALIASES.get(raw_equiv)
    if equiv is None:
        equiv = (
            "same_goal_different_approach" if same_root_cause
            else "addresses_different_cause"
        )
    verdict = _VERDICT_ALIASES.get(raw_verdict) or _EQUIV_TO_VERDICT[equiv]
    if verdict != raw_verdict or equiv != raw_equiv:
        log.warning(
            "analysis.compare.normalized",
            raw_verdict=raw_verdict, raw_equivalence=raw_equiv,
            verdict=verdict, equivalence=equiv,
        )
    return verdict, equiv


@dataclass(frozen=True)
class FixComparison:
    verdict: str
    same_root_cause: bool
    files_both: list[str]
    files_ours_only: list[str]
    files_theirs_only: list[str]
    equivalence: str
    rationale: str
    confidence: float




def compare_fix_to_pr(
    issue_title: str,
    root_cause_summary: str,
    our_fix: FixProposal,
    pr_ref: PullRequestRef,
    pr_diff: PrDiff,
    router,
) -> tuple[FixComparison, RoutingDecision]:
    ours = {c.path for c in our_fix.changes}
    theirs = {f.filename for f in pr_diff.files}
    files_both = sorted(ours & theirs)
    files_ours_only = sorted(ours - theirs)
    files_theirs_only = sorted(theirs - ours)

    our_blob = (
        "\n\n".join(f"--- {c.path}\n{c.diff or c.patched_content[:800]}" for c in our_fix.changes)
        or "(no changes)"
    )
    their_blob = "\n\n".join(f"--- {f.filename}\n{f.patch}" for f in pr_diff.files) or "(none)"
    prompt = (
        f"ISSUE: {issue_title}\nAGENT ROOT CAUSE: {root_cause_summary}\n\n"
        f"AGENT FIX:\n{our_blob}\n\n"
        f"HUMAN PR #{pr_ref.number}:\n{their_blob}\n\n"
        f"File overlap (computed): both={files_both}, agent_only={files_ours_only}, "
        f"human_only={files_theirs_only}\n\n{_SCHEMA_HINT}"
    )
    response, decision = router.run(
        TaskType.FIX_COMPARISON,
        prompt,
        sensitivity=Sensitivity.INTERNAL,
        system=FIX_COMPARISON_SYSTEM,
    )
    parsed = extract_json(response.text)
    if parsed is None:
        log.warning("analysis.compare.unparseable", pr=pr_ref.number)
        return (
            FixComparison(
                verdict="divergent",
                same_root_cause=False,
                files_both=files_both,
                files_ours_only=files_ours_only,
                files_theirs_only=files_theirs_only,
                equivalence="addresses_different_cause",
                rationale="Comparison response could not be parsed.",
                confidence=0.0,
            ),
            decision,
        )

    same_root_cause = bool(parsed.get("same_root_cause", False))
    verdict, equiv = _normalize(
        str(parsed.get("verdict", "")).lower(),
        str(parsed.get("equivalence", "")).lower(),
        same_root_cause,
    )
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        FixComparison(
            verdict=verdict,
            same_root_cause=same_root_cause,
            files_both=files_both,
            files_ours_only=files_ours_only,
            files_theirs_only=files_theirs_only,
            equivalence=equiv,
            rationale=str(parsed.get("rationale", "")),
            confidence=max(0.0, min(1.0, confidence)),
        ),
        decision,
    )
