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


def retrieve_code_files(
    ctx: AgentContext, paths: list[str], *, max_files: int = 5
) -> dict[str, str]:
    """Fetch contents for ``paths``; return a {path: content} mapping.

    Skips paths the code host can't return (logged as misses). The mapping
    preserves insertion order, so the first suspected file stays first.
    """
    files: dict[str, str] = {}
    for path in paths[:max_files]:
        try:
            files[path] = ctx.code_host.get_file(path)
        except Exception as exc:
            log.warning("tool.code_retrieval.miss", path=path, error=str(exc))
    log.info("tool.code_retrieval", requested=len(paths), retrieved=len(files))
    return files


def format_code_for_prompt(files: dict[str, str]) -> str:
    """Render a ``{path: content}`` mapping as a single labelled blob for a prompt."""
    if not files:
        return ""
    return "\n\n".join(f"# ── {path} ──\n{content}" for path, content in files.items())


def retrieve_code(ctx: AgentContext, paths: list[str], *, max_files: int = 5) -> str:
    """Back-compat wrapper: returns the prompt-formatted blob."""
    return format_code_for_prompt(retrieve_code_files(ctx, paths, max_files=max_files))


def list_dir(ctx: AgentContext, path: str) -> list[str]:
    """List files under ``path`` in the target repo; ``[]`` on any failure."""
    try:
        entries = ctx.code_host.list_dir(path)
    except Exception as exc:
        log.warning("tool.list_dir.failed", path=path, error=str(exc))
        return []
    log.info("tool.list_dir", path=path, count=len(entries))
    return entries
