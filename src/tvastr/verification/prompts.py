"""System prompts and prompt builders for the verification loop."""

from __future__ import annotations

from tvastr.domain import FailurePattern, FixRegister, LogEvent, RootCause
from tvastr.verification.models import BEHAVIOR_OK_MARKER

REPRO_SYSTEM = (
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

REPAIR_SYSTEM = (
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

CRITIQUE_SYSTEM = (
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

REGISTER_GUIDANCE: dict[FixRegister, str] = {
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


def build_repro_prompt(
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
    return base + REGISTER_GUIDANCE.get(register, "")
