"""Tool: retrieve source files and extract file references from stack traces."""

from __future__ import annotations

import re

from tvastr.agent.context import AgentContext
from tvastr.domain import LogEvent
from tvastr.logging import get_logger

log = get_logger(__name__)

_STACK_FILE_RE = re.compile(r'File "([^"]+\.py)"')


def extract_stack_files(events: list[LogEvent]) -> list[str]:
    """Pull unique .py file paths out of the events' stack traces (in order)."""
    files: list[str] = []
    for event in events:
        for match in _STACK_FILE_RE.finditer(event.stack_trace or ""):
            path = match.group(1)
            if path not in files:
                files.append(path)
    return files


def retrieve_code(ctx: AgentContext, paths: list[str], *, max_files: int = 5) -> str:
    """Fetch the contents of ``paths`` and concatenate into a single context blob."""
    chunks: list[str] = []
    for path in paths[:max_files]:
        try:
            content = ctx.code_host.get_file(path)
        except Exception as exc:
            log.warning("tool.code_retrieval.miss", path=path, error=str(exc))
            continue
        chunks.append(f"# ── {path} ──\n{content}")
    log.info("tool.code_retrieval", requested=len(paths), retrieved=len(chunks))
    return "\n\n".join(chunks)
