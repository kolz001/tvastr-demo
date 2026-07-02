"""LLM analysis of a discovered PR: what it changes, whether it addresses the
issue, and the approach. One cloud call; tolerant of unparseable responses."""

from __future__ import annotations

from dataclasses import dataclass

from tvastr.analysis._jsonutil import extract_json
from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.analysis.prompts import PR_ANALYSIS_SYSTEM
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SCHEMA_HINT = (
    'Return JSON: {"addresses_issue": "yes"|"partial"|"no", '
    '"approach_summary": "<=3 sentences", "key_files": ["path", ...], '
    '"root_cause": "1-2 sentences"}'
)
_VALID = {"yes", "partial", "no"}


@dataclass(frozen=True)
class PrAnalysis:
    addresses_issue: str  # yes | partial | no | unknown
    approach_summary: str
    key_files: list[str]
    root_cause: str
    pr_number: int
    pr_state: str
    raw: str = ""


def _diff_blob(diff: PrDiff) -> str:
    parts = [
        f"--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions})\n{f.patch}"
        for f in diff.files
    ]
    blob = "\n\n".join(parts) or "(no diff available)"
    if diff.truncated:
        blob += "\n\n(NOTE: diff truncated for length.)"
    return blob


def analyze_pr(
    issue_title: str,
    issue_body: str | None,
    pr_ref: PullRequestRef,
    pr_diff: PrDiff,
    router,
) -> tuple[PrAnalysis, RoutingDecision]:
    prompt = (
        f"ISSUE: {issue_title}\n\n{(issue_body or '')[:2000]}\n\n"
        f"CANDIDATE PR #{pr_ref.number} ({pr_ref.state}): {pr_ref.title}\n\n"
        f"DIFF:\n{_diff_blob(pr_diff)}\n\n{_SCHEMA_HINT}"
    )
    response, decision = router.run(
        TaskType.PR_ANALYSIS, prompt, sensitivity=Sensitivity.INTERNAL, system=PR_ANALYSIS_SYSTEM
    )
    parsed = extract_json(response.text)
    if parsed is None:
        log.warning("analysis.analyze_pr.unparseable", pr=pr_ref.number)
        analysis = PrAnalysis(
            addresses_issue="unknown",
            approach_summary=response.text.strip()[:500],
            key_files=[],
            root_cause="",
            pr_number=pr_ref.number,
            pr_state=pr_ref.state,
            raw=response.text,
        )
        return analysis, decision

    verdict = str(parsed.get("addresses_issue", "")).lower()
    analysis = PrAnalysis(
        addresses_issue=verdict if verdict in _VALID else "unknown",
        approach_summary=str(parsed.get("approach_summary", "")),
        key_files=[str(p) for p in parsed.get("key_files", []) if p],
        root_cause=str(parsed.get("root_cause", "")),
        pr_number=pr_ref.number,
        pr_state=pr_ref.state,
        raw=response.text,
    )
    return analysis, decision
