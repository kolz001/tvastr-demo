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
    evidence_source: str  # stack_trace | search | none — how suspected_files were found
    need_more_context: bool  # reasoning asked for another retrieval round
    next_targets: dict  # {"queries": list[str], "paths": list[str]} from reasoning
    retrieval_iterations: int  # number of expand_context rounds run
    retrieved_paths: set[str]  # paths fetched + queries issued, for cross-round dedup
    root_cause: RootCause
    fix: FixProposal
    pr_draft: PullRequestDraft
    pr_result: PullRequestResult
    routing: list[RoutingDecision]
    outcome: str  # pr_opened | dry_run | skipped | failed
    notes: str
    pr_ref: PullRequestRef | None
    pr_diff: PrDiff | None
    fix_comparison: FixComparison | None
    doc_sources: list[str]  # citation URLs from documentation grounding
