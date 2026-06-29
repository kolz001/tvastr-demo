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

log = get_logger(__name__)

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

_REGISTER_GUIDANCE: dict[FixRegister, str] = {
    FixRegister.WARN: (
        "\n\nFIX REGISTER = WARN: the fix does NOT change behavior; it emits a "
        "warning on the failing input. Write a BEHAVIORAL reproducer that triggers "
        "the failing input INSIDE:\n"
        "    import warnings\n"
        "    with warnings.catch_warnings(record=True) as _w:\n"
        "        warnings.simplefilter('always')\n"
        "        <trigger the failing input>\n"
        "then assert at least one captured warning's message references the failing "
        "symbol/field, and end with the marker. The bug = NO such warning at baseline."
    ),
    FixRegister.BETTER_ERROR: (
        "\n\nFIX REGISTER = BETTER_ERROR: the fix replaces a cryptic failure with a "
        "clearer error. Write a BEHAVIORAL reproducer that triggers the failing input "
        "inside try/except, asserts the raised error's type or message references the "
        "failing symbol/field (the clearer error), and ends with the marker. Use stdlib "
        "only — no pytest."
    ),
}


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
    "PREFER a BEHAVIORAL reproducer: set up REAL, valid input, exercise the FULL "
    "operation end-to-end, and ASSERT the user-visible output reflects that input "
    "(e.g. data round-trips: what you get back equals what you put in) or a stated "
    "expected value. End the script with exactly:\n"
    f"    print(\"{BEHAVIOR_OK_MARKER}\")\n"
    "so the marker prints ONLY if every assertion passed.\n\n"
    "CRITICAL - make the oracle strong:\n"
    "- Do NOT mock or hand-construct the already-broken/degraded intermediate "
    "state and then assert that degraded output. Drive the real operation with "
    "valid input instead.\n"
    "- Asserting an empty/None/degenerate result is FORBIDDEN unless empty is "
    "genuinely the correct outcome for valid input.\n"
    "- A fix that merely SUPPRESSES the error (returns empty/default without "
    "restoring behavior) MUST FAIL your assertion.\n\n"
    "If you CANNOT determine the expected correct behavior from the issue and "
    "code, FALL BACK to a crash reproducer that simply re-triggers the original "
    "exception (no assertion, no marker).\n\n"
    "The FIRST line of your response MUST be one of:\n"
    "    # tvastr-kind: behavioral\n"
    "    # tvastr-kind: crash\n"
    "If actual source of the suspected files is shown, use ONLY APIs that appear "
    "in it; do not invent constructor parameters. Respond with ONLY Python source "
    "- no prose, no markdown fences."
    " The issue's integration and its dependencies are installed in the run "
    "sandbox: import and construct the REAL classes named in the traceback "
    "(response/SDK objects) rather than defining fake/stub classes for external "
    "library types — fakes (e.g. supporting [] but not dict()) won't match real "
    "behavior and will break under the fix."
)

_REPAIR_SYSTEM = (
    "You are fixing a REPRODUCER that failed in its OWN code, not in the library "
    "under test — so it is untrustworthy. The issue's integration package and its "
    "dependencies ARE installed in the run sandbox. Import and construct the REAL "
    "objects named in the traceback (e.g. `from ollama import GenerateResponse`); "
    "do NOT define fake/stub classes for external library types — fakes don't match "
    "real behavior (e.g. a hand-rolled response that supports `[]` but not `dict()`). "
    "Keep the same reproducer KIND. The FIRST line must be '# tvastr-kind: behavioral' "
    "or '# tvastr-kind: crash'; if behavioral, end with the success marker. Respond "
    "with ONLY Python source — no prose, no fences."
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
    register: FixRegister = FixRegister.REPAIR,
) -> str:
    sample = sample_events[0] if sample_events else None
    trace = sample.stack_trace if sample and sample.stack_trace else "(no traceback available)"
    code_section = (
        f"Actual source of the suspected files (use only these APIs):\n{code_context}\n\n"
        if code_context.strip()
        else ""
    )
    base = (
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
    return base + _REGISTER_GUIDANCE.get(register, "")


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


_CRITIQUE_SYSTEM = (
    "You audit a Python reproducer used to verify a bug fix. The reproducer must "
    "FAIL on a fix that merely SUPPRESSES the error and PASS only when the intended "
    "behavior is restored. If a fix that just suppresses the exception (returns "
    "empty/default/None without restoring the real result) would STILL pass its "
    "assertions, the oracle is TOO WEAK: rewrite it to set up REAL valid input, "
    "exercise the full operation end-to-end, and assert the output matches that "
    "input (round-trip) or a documented expected value. Do NOT mock/hand-construct "
    "the already-broken state and assert it. Keep the first line "
    "`# tvastr-kind: behavioral` and end with the exact marker print. If the "
    "reproducer is already strong, return it UNCHANGED. Respond with ONLY Python "
    "source - no prose, no markdown fences."
)


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
            system=_CRITIQUE_SYSTEM,
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
    prompt = _build_prompt(
        pattern, root_cause, sample_events, code_context=_truncate(resolved_context),
        register=register,
    )
    response, _ = router.run(
        TaskType.FIX_GENERATION,
        prompt,
        sensitivity=pattern.sensitivity,
        system=_SYSTEM,
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
            system=_REPAIR_SYSTEM,
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
