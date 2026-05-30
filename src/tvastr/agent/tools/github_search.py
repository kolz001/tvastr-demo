"""Tool: search the target repository for code relevant to a failure."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.logging import get_logger

log = get_logger(__name__)


def search_codebase(ctx: AgentContext, query: str, *, limit: int = 5) -> list[str]:
    """Return candidate file paths in the target repo matching ``query``."""
    log.info("tool.github_search", query=query, limit=limit)
    return ctx.code_host.search_code(query, limit=limit)
