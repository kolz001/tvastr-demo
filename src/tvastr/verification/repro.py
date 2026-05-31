"""Synthesize a reproducer for an issue.

Strategy: try to extract a code block from the issue body first (cheapest,
highest fidelity). Fall back to Claude synthesis when the body has no usable
block. The result is always a ``Reproducer`` whose ``code`` is a single Python
snippet the sandbox can run with ``python -``.
"""

from __future__ import annotations

import ast
import re

from tvastr.domain import FailurePattern, LogEvent, RootCause
from tvastr.llm.router import HybridRouter, TaskType
from tvastr.logging import get_logger
from tvastr.verification.models import Reproducer, ReproducerSource

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
    "You are a senior Python engineer. Produce a minimal reproducer for the "
    "failure described below — 5 to 15 lines of Python that, when run on a "
    "clean install of the target project, re-triggers the original exception. "
    "Respond with ONLY the Python source — no prose, no markdown fences."
)


def _build_prompt(
    pattern: FailurePattern, root_cause: RootCause, sample_events: list[LogEvent]
) -> str:
    sample = sample_events[0] if sample_events else None
    trace = sample.stack_trace if sample and sample.stack_trace else "(no traceback available)"
    return (
        f"Failure title: {pattern.title}\n"
        f"Exception type: {pattern.exception_type or '(unknown)'}\n"
        f"Representative message: {pattern.representative_message}\n"
        f"Root cause analysis: {root_cause.summary}\n"
        f"Suspected files: {', '.join(root_cause.suspected_files) or '(unknown)'}\n\n"
        f"Traceback excerpt:\n{trace[:2000]}\n\n"
        "Write the reproducer."
    )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:python|py)?\s*|\s*```$", "", stripped, flags=re.DOTALL).strip()
    return stripped


def synthesize_reproducer(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    issue_body: str | None,
    router: HybridRouter,
) -> Reproducer:
    """Best-effort: body → Claude. Returns the lower-cost option that works."""
    if (extracted := extract_from_body(issue_body or "")) is not None:
        log.info("verify.repro.extracted", lines=extracted.count("\n") + 1)
        return Reproducer(
            source=ReproducerSource.ISSUE_BODY,
            code=extracted,
            expected_exception=pattern.exception_type,
        )

    prompt = _build_prompt(pattern, root_cause, sample_events)
    response, _ = router.run(
        TaskType.FIX_GENERATION,
        prompt,
        sensitivity=pattern.sensitivity,
        system=_SYSTEM,
    )
    code = _strip_fences(response.text)
    log.info("verify.repro.synthesized", lines=code.count("\n") + 1, model=response.model)
    return Reproducer(
        source=ReproducerSource.CLAUDE,
        code=code,
        expected_exception=pattern.exception_type,
    )
