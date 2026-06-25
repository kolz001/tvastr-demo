"""Synthesize a reproducer for an issue.

Strategy: try to extract a code block from the issue body first (cheapest,
highest fidelity). Fall back to Claude synthesis when the body has no usable
block. The result is always a ``Reproducer`` whose ``code`` is a single Python
snippet the sandbox can run with ``python -``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

from tvastr.domain import FailurePattern, LogEvent, RootCause
from tvastr.llm.router import HybridRouter, TaskType
from tvastr.logging import get_logger
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    ReproducerSource,
)

log = get_logger(__name__)

# Greedy match of fenced python blocks. We accept ```python or ```py or just ```.
_CODE_BLOCK_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# When extracting, prefer blocks that look like a runnable repro (imports +
# something that exercises them) rather than a traceback transcript.
_TRACEBACK_HINT = "Traceback (most recent call last)"


def _looks_runnable(code: str) -> bool:
    """Cheap heuristic: parses as Python and exercises *something*.

    A bare docstring counts as ``ast.Expr`` but isn't useful for a reproducer,
    so an ``Expr`` only counts when its value is a ``Call`` (i.e. it actually
    does something). Imports always count even without a call site.
    """
    if _TRACEBACK_HINT in code:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef)):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
    return False


def extract_from_body(body: str) -> str | None:
    """Pull the first runnable-looking python block out of an issue body."""
    if not body:
        return None
    for match in _CODE_BLOCK_RE.finditer(body):
        candidate = match.group(1).strip()
        if _looks_runnable(candidate):
            return candidate
    return None


_SYSTEM = (
    "You are a senior Python engineer. Produce a MINIMAL reproducer (5-20 lines) "
    "for the failure below, runnable on a clean install of the target project.\n\n"
    "PREFER a BEHAVIORAL reproducer: set up the scenario, exercise the buggy "
    "operation, and ASSERT the expected CORRECT result (e.g. data round-trips, "
    "the returned value equals what was stored). End the script with exactly:\n"
    f"    print(\"{BEHAVIOR_OK_MARKER}\")\n"
    "so the marker prints ONLY if every assertion passed. A fix that merely "
    "suppresses the error without restoring behavior must fail your assertion.\n\n"
    "If you CANNOT determine the expected correct behavior from the issue and "
    "code, FALL BACK to a crash reproducer that simply re-triggers the original "
    "exception (no assertion, no marker).\n\n"
    "The FIRST line of your response MUST be one of:\n"
    "    # tvastr-kind: behavioral\n"
    "    # tvastr-kind: crash\n"
    "If actual source of the suspected files is shown, use ONLY APIs that appear "
    "in it; do not invent constructor parameters. Respond with ONLY Python source "
    "— no prose, no markdown fences."
)


def _truncate(text: str, max_lines: int = 200) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n# … ({len(lines) - max_lines} more lines elided)"


def _build_prompt(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    code_context: str = "",
) -> str:
    sample = sample_events[0] if sample_events else None
    trace = sample.stack_trace if sample and sample.stack_trace else "(no traceback available)"
    code_section = (
        f"Actual source of the suspected files (use only these APIs):\n{code_context}\n\n"
        if code_context.strip()
        else ""
    )
    return (
        f"Failure title: {pattern.title}\n"
        f"Exception type: {pattern.exception_type or '(unknown)'}\n"
        f"Representative message: {pattern.representative_message}\n"
        f"Root cause analysis: {root_cause.summary}\n"
        f"Suspected files: {', '.join(root_cause.suspected_files) or '(unknown)'}\n\n"
        f"Traceback excerpt:\n{trace[:2000]}\n\n"
        f"{code_section}"
        f"Start your response with # tvastr-kind: behavioral or # tvastr-kind: crash.\n"
        f"If behavioral, end with print(\"{BEHAVIOR_OK_MARKER}\").\n"
        "Write the reproducer."
    )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:python|py)?\s*|\s*```$", "", stripped, flags=re.DOTALL).strip()
    return stripped


def _parse_kind(code: str) -> ReproducerKind:
    """Read the leading `# tvastr-kind:` tag; default CRASH when absent/unknown."""
    first = code.lstrip().splitlines()[0] if code.strip() else ""
    if "tvastr-kind:" in first and "behavioral" in first:
        return ReproducerKind.BEHAVIORAL
    return ReproducerKind.CRASH


def synthesize_reproducer(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    issue_body: str | None,
    router: HybridRouter,
    code_context: str | Callable[[], str] = "",
) -> Reproducer:
    """Best-effort: body → Claude. Returns the lower-cost option that works.

    ``code_context`` is the actual source of the suspected files (formatted by
    :func:`tvastr.agent.tools.code_retrieval.format_code_for_prompt`). When
    provided, the prompt instructs Claude to use only APIs visible in that
    source — the cheapest way to reduce hallucinated constructor params. A
    callable is resolved lazily, only on the Claude path — fetching source is
    wasted work when the issue body already contains a runnable block.
    """
    if (extracted := extract_from_body(issue_body or "")) is not None:
        log.info("verify.repro.extracted", lines=extracted.count("\n") + 1)
        return Reproducer(
            source=ReproducerSource.ISSUE_BODY,
            code=extracted,
            expected_exception=pattern.exception_type,
        )

    resolved_context = code_context() if callable(code_context) else code_context
    prompt = _build_prompt(
        pattern, root_cause, sample_events, code_context=_truncate(resolved_context)
    )
    response, _ = router.run(
        TaskType.FIX_GENERATION,
        prompt,
        sensitivity=pattern.sensitivity,
        system=_SYSTEM,
    )
    code = _strip_fences(response.text)
    kind = _parse_kind(code)
    log.info(
        "verify.repro.synthesized",
        lines=code.count("\n") + 1,
        model=response.model,
        kind=kind.value,
    )
    return Reproducer(
        source=ReproducerSource.CLAUDE,
        code=code,
        expected_exception=pattern.exception_type,
        kind=kind,
    )
