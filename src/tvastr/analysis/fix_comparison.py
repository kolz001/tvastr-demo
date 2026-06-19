"""Grade tvastr's generated fix against the maintainer's PR (ground truth).

File overlap is computed deterministically in code; the LLM judges root-cause
agreement, functional equivalence, the overall verdict, and a rationale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.domain import FixProposal, RoutingDecision, Sensitivity
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You compare an autonomous agent's proposed fix against a human maintainer's "
    "pull request for the same bug. Judge whether they target the same root cause "
    "and are functionally equivalent. Respond ONLY with JSON."
)
_SCHEMA_HINT = (
    'Return JSON: {"verdict": "match"|"partial"|"divergent", '
    '"same_root_cause": true|false, '
    '"equivalence": "functionally_equivalent"|"same_goal_different_approach"|'
    '"addresses_different_cause", "rationale": "2-4 sentences", '
    '"confidence": 0.0-1.0}'
)
_VERDICTS = {"match", "partial", "divergent"}
_EQUIV = {
    "functionally_equivalent",
    "same_goal_different_approach",
    "addresses_different_cause",
}


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


def _extract_json(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return None


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
        TaskType.FIX_COMPARISON, prompt, sensitivity=Sensitivity.INTERNAL, system=_SYSTEM
    )
    parsed = _extract_json(response.text)
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

    verdict = str(parsed.get("verdict", "")).lower()
    equiv = str(parsed.get("equivalence", "")).lower()
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        FixComparison(
            verdict=verdict if verdict in _VERDICTS else "divergent",
            same_root_cause=bool(parsed.get("same_root_cause", False)),
            files_both=files_both,
            files_ours_only=files_ours_only,
            files_theirs_only=files_theirs_only,
            equivalence=equiv if equiv in _EQUIV else "addresses_different_cause",
            rationale=str(parsed.get("rationale", "")),
            confidence=max(0.0, min(1.0, confidence)),
        ),
        decision,
    )
