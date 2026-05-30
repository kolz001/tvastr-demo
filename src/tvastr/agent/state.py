"""Mutable state threaded through the remediation graph.

LangGraph merges each node's returned partial dict into this state by key, so
nodes only return the fields they produce.
"""

from __future__ import annotations

from typing import TypedDict

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
