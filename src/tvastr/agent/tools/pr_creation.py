"""Tool: open a pull request on the target repository."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.logging import get_logger

log = get_logger(__name__)


def open_pull_request(ctx: AgentContext, draft: PullRequestDraft) -> PullRequestResult:
    log.info("tool.pr_creation", branch=draft.branch, title=draft.title)
    return ctx.code_host.open_pull_request(draft)
