"""GET /api/issues — fetch the top N bug-labeled issues for a repo, ranked.

Sort options mirror the GitHub Search API. Falls back to the mock fetcher when
``use_mocks=True`` or no token is present, so the UI works fully offline.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from tvastr.config import get_settings
from tvastr.ingestion.comments import assess_issue
from tvastr.ingestion.github_issues import (
    GitHubIssuesFetcher,
    IssueRecord,
    MockGitHubIssuesFetcher,
)

router = APIRouter(tags=["issues"])


SortKey = Literal["reactions-+1", "comments", "interactions", "reactions", "created"]


class IssueOut(BaseModel):
    number: int
    title: str
    body_preview: str
    url: str
    labels: list[str]
    reactions: int
    thumbs_up: int
    comments: int


class IssueList(BaseModel):
    repo: str
    sort: SortKey
    label: str
    mode: Literal["live", "mock"]
    auto_analyze_prs: bool  # whether the UI should auto-analyze the top-5 PRs on load
    issues: list[IssueOut]


def _to_out(rec: IssueRecord) -> IssueOut:
    body = (rec.body or "").strip().splitlines()
    preview = " ".join(line.strip() for line in body[:4] if line.strip())[:280]
    return IssueOut(
        number=rec.number,
        title=rec.title,
        body_preview=preview,
        url=rec.url,
        labels=rec.labels,
        reactions=rec.reactions,
        thumbs_up=rec.thumbs_up,
        comments=rec.comments,
    )


@router.get("/api/issues", response_model=IssueList)
def list_issues(
    repo: str = Query(default="run-llama/llama_index"),
    sort: SortKey = Query(default="reactions-+1"),
    label: str = Query(default="bug"),
    limit: int = Query(default=20, ge=1, le=100),
) -> IssueList:
    settings = get_settings()
    use_mock = settings.use_mocks or not settings.github_token

    fetcher: GitHubIssuesFetcher | MockGitHubIssuesFetcher
    if use_mock:
        fetcher = MockGitHubIssuesFetcher(repo)
    else:
        fetcher = GitHubIssuesFetcher(repo, token=settings.github_token)

    records = fetcher.fetch(label=label, limit=limit, sort=sort)
    return IssueList(
        repo=repo,
        sort=sort,
        label=label,
        mode="mock" if use_mock else "live",
        auto_analyze_prs=settings.auto_analyze_prs,
        issues=[_to_out(r) for r in records],
    )


class ResolutionOut(BaseModel):
    confidence: Literal["high", "medium", "low", "none"]
    label: str
    signals: list[str]
    comment_count: int
    days_since_last_comment: int | None


@router.get("/api/resolution", response_model=ResolutionOut)
def issue_resolution(
    repo: str = Query(...),
    number: int = Query(..., ge=1),
) -> ResolutionOut:
    """Return a cheap heuristic assessment of whether an open issue looks
    resolved per comments. Powers the triage UI's chip.

    Mock mode (no token) always returns ``confidence='none'`` because the
    issue list is synthetic and there are no real comments to assess.
    """
    settings = get_settings()
    assessment = assess_issue(
        repo,
        number,
        token=settings.github_token,
        use_mocks=settings.use_mocks,
    )
    return ResolutionOut(**asdict(assessment))
