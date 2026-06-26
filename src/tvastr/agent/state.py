"""Mutable state threaded through the remediation graph.

LangGraph merges each node's returned partial dict into this state by key, so
nodes only return the fields they produce.
"""

from __future__ import annotations

from typing import TypedDict

from tvastr.analysis.fix_comparison import FixComparison
from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.domain import (
    FailurePattern,
    FixProposal,
    LogEvent,
    PullRequestDraft,
    PullRequestResult,
    RootCause,
    RoutingDecision,
)


class AgentState(TypedDict, total=False):
    pattern: FailurePattern
    sample_events: list[LogEvent]
    code_context: str
    code_files: dict[str, str]
    suspected_files: list[str]
    root_cause: RootCause
    fix: FixProposal
    pr_draft: PullRequestDraft
    pr_result: PullRequestResult
    routing: list[RoutingDecision]
    outcome: str  # pr_opened | dry_run | skipped | failed
    notes: str
    pr_ref: PullRequestRef | None
    pr_diff: PrDiff | None
    issue_body: str | None  # full issue text, for the investigator's starting context
    fix_comparison: FixComparison | None
    doc_sources: list[str]  # citation URLs from documentation grounding
