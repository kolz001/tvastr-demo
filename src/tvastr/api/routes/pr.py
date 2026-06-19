"""PR discovery + analysis endpoints for the triage UI.

GET  /api/issue-pr     cheap: find the PR addressing an issue (no LLM)
POST /api/pr-analysis  one cloud LLM call analyzing that PR
Both return null/unavailable gracefully in mock mode or when no PR exists.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from tvastr.analysis.pr_analysis import analyze_pr
from tvastr.analysis.pr_discovery import discover_pr, fetch_pr_diff
from tvastr.config import get_settings
from tvastr.ingestion.comments import fetch_issue_body
from tvastr.llm.router import build_router

router = APIRouter(tags=["pr"])


class PrRefOut(BaseModel):
    number: int
    title: str
    state: str
    merged: bool
    url: str


class IssuePrOut(BaseModel):
    pr: PrRefOut | None


class PrAnalysisRequest(BaseModel):
    repo: str
    number: int


class PrAnalysisOut(BaseModel):
    pr: PrRefOut | None
    addresses_issue: str | None = None
    approach_summary: str | None = None
    key_files: list[str] = []
    root_cause: str | None = None


@router.get("/api/issue-pr", response_model=IssuePrOut)
def issue_pr(repo: str, number: int) -> IssuePrOut:
    s = get_settings()
    ref = discover_pr(repo, number, token=s.github_token, use_mocks=s.use_mocks)
    if ref is None:
        return IssuePrOut(pr=None)
    return IssuePrOut(
        pr=PrRefOut(
            number=ref.number,
            title=ref.title,
            state=ref.state,
            merged=ref.merged,
            url=ref.url,
        )
    )


@router.post("/api/pr-analysis", response_model=PrAnalysisOut)
def pr_analysis(req: PrAnalysisRequest) -> PrAnalysisOut:
    s = get_settings()
    if s.use_mocks or not s.github_token:
        return PrAnalysisOut(pr=None)

    try:
        ref = discover_pr(req.repo, req.number, token=s.github_token, use_mocks=s.use_mocks)
        if ref is None:
            return PrAnalysisOut(pr=None)
        diff = fetch_pr_diff(req.repo, ref.number, token=s.github_token)
        body = fetch_issue_body(req.repo, req.number, token=s.github_token)
        router_ = build_router(s)
        analysis, _ = analyze_pr(f"#{req.number}", body, ref, diff, router_)
    except Exception:
        return PrAnalysisOut(pr=None)
    return PrAnalysisOut(
        pr=PrRefOut(
            number=ref.number,
            title=ref.title,
            state=ref.state,
            merged=ref.merged,
            url=ref.url,
        ),
        addresses_issue=analysis.addresses_issue,
        approach_summary=analysis.approach_summary,
        key_files=analysis.key_files,
        root_cause=analysis.root_cause,
    )
