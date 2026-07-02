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

from tvastr.domain import FailurePattern, FixRegister, LogEvent, RootCause
from tvastr.llm.router import HybridRouter, TaskType
from tvastr.logging import get_logger
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    ReproducerSource,
)
from tvastr.verification.prompts import (
    CRITIQUE_SYSTEM,
    REPAIR_SYSTEM,
    REPRO_SYSTEM,
    build_repro_prompt,
)

log = get_logger(__name__)

# Compatibility aliases: tests/test_repro.py and tests/test_repro_register.py
# import these private names from this module.
_SYSTEM = REPRO_SYSTEM
_build_prompt = build_repro_prompt

# Greedy match of fenced python blocks. We accept ```python or ```py or just ```.
_CODE_BLOCK_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# Match the tvastr-kind tag and capture its value.
_KIND_TAG_RE = re.compile(r"#\s*tvastr-kind:\s*(\w+)")

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


def _truncate(text: str, max_lines: int = 200) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n# … ({len(lines) - max_lines} more lines elided)"


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:python|py)?\s*|\s*```$", "", stripped, flags=re.DOTALL).strip()
    return stripped


def _parse_kind(code: str) -> ReproducerKind:
    """Read the leading `# tvastr-kind:` tag's value; default CRASH when absent/unknown."""
    first = code.lstrip().splitlines()[0] if code.strip() else ""
    m = _KIND_TAG_RE.match(first.strip())
    if m and m.group(1).lower() == "behavioral":
        return ReproducerKind.BEHAVIORAL
    return ReproducerKind.CRASH


def _critique_reproducer(
    code: str,
    pattern: FailurePattern,
    root_cause: RootCause,
    code_context: str,
    router: HybridRouter,
) -> str:
    """One adversarial pass that strengthens a weak behavioral reproducer.

    Returns the (possibly rewritten) code. On any failure, or a malformed
    rewrite, returns ``code`` unchanged — the critique strengthens, never blocks.
    """
    code_section = (
        f"Source of suspected files:\n{_truncate(code_context)}\n\n"
        if code_context.strip()
        else ""
    )
    prompt = (
        f"Failure: {pattern.title}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"{code_section}"
        f"Reproducer under audit:\n{code}\n\n"
        "Audit it. If a suppress-only fix would still pass its assertions, rewrite "
        "it to assert the intended behavior; otherwise return it unchanged."
    )
    try:
        response, _ = router.run(
            TaskType.REPRO_CRITIQUE,
            prompt,
            sensitivity=pattern.sensitivity,
            system=CRITIQUE_SYSTEM,
        )
    except Exception as exc:
        log.warning("verify.repro.critique_failed", error=str(exc))
        return code
    revised = _strip_fences(response.text)
    if not revised.strip():
        return code
    # A behavioral rewrite that dropped the success marker is malformed — keep the
    # original. An explicit downgrade to `# tvastr-kind: crash` is allowed.
    if _parse_kind(revised) == ReproducerKind.BEHAVIORAL and BEHAVIOR_OK_MARKER not in revised:
        log.info("verify.repro.critique_discarded", reason="behavioral rewrite lost the marker")
        return code
    log.info("verify.repro.critiqued", changed=(revised != code))
    return revised


def synthesize_reproducer(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    issue_body: str | None,
    router: HybridRouter,
    code_context: str | Callable[[], str] = "",
    register: FixRegister = FixRegister.REPAIR,
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
    prompt = build_repro_prompt(
        pattern, root_cause, sample_events, code_context=_truncate(resolved_context),
        register=register,
    )
    response, _ = router.run(
        TaskType.FIX_GENERATION,
        prompt,
        sensitivity=pattern.sensitivity,
        system=REPRO_SYSTEM,
    )
    code = _strip_fences(response.text)
    kind = _parse_kind(code)
    # The critique hardens BEHAVIORAL round-trip oracles; WARN/BETTER_ERROR
    # assertions are intentionally non-behavioral (a warning/clear error, NOT a
    # restored result), so the round-trip-biased critique must not rewrite them.
    if kind == ReproducerKind.BEHAVIORAL and register not in (
        FixRegister.WARN,
        FixRegister.BETTER_ERROR,
    ):
        code = _critique_reproducer(code, pattern, root_cause, resolved_context, router)
        kind = _parse_kind(code)  # re-parse: a rewrite keeps or restates the tag
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


def repair_reproducer(
    repro: Reproducer,
    evidence: dict,
    pattern: FailurePattern,
    root_cause: RootCause,
    router: HybridRouter,
) -> Reproducer:
    """Re-synthesize a reproducer that failed in its own scaffolding, using the
    real installed deps. Returns the original ``repro`` on any error."""
    error = str(evidence.get("rerun_stderr_tail") or evidence.get("hint") or "")
    expected = repro.expected_exception or pattern.exception_type or "(unknown)"
    prompt = (
        f"The issue: {pattern.title}\nExpected symptom: {expected}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"This reproducer FAILED in its own code (not the library under test):\n"
        f"```\n{repro.code}\n```\n\n"
        f"The real error from running it:\n{error[:1500]}\n\n"
        f"Rewrite it to use the REAL installed objects and reproduce the actual symptom."
    )
    try:
        response, _ = router.run(
            TaskType.FIX_GENERATION, prompt, sensitivity=pattern.sensitivity,
            system=REPAIR_SYSTEM,
        )
        code = _strip_fences(response.text)
        if not code.strip():
            return repro
        return Reproducer(
            source=ReproducerSource.CLAUDE,
            code=code,
            expected_exception=pattern.exception_type,
            kind=_parse_kind(code),
        )
    except Exception as exc:
        log.warning("verify.repro.repair_failed", error=str(exc))
        return repro
