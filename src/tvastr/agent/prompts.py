"""System prompts and prompt builders for the remediation agent."""

from __future__ import annotations

INVESTIGATE_SYSTEM = (
    "You are a senior engineer debugging a reported bug by reading the codebase. "
    "Follow this method strictly:\n"
    "1. ROOT CAUSE FIRST: do not conclude until you have READ the actual code that "
    "proves the cause; if you have not, keep investigating or report low confidence.\n"
    "2. VERIFY THE REPORTER'S HYPOTHESIS: identify the real symptom AND any cause the "
    "reporter guessed, and treat the guess as a hypothesis to confirm against the code, "
    "not as fact.\n"
    "3. CROSS-REFERENCE: compare related/sibling code paths (read vs write vs delete) and "
    "look for the inconsistency that explains the bug.\n"
    "4. CITE EVIDENCE: your root_cause must reference specific file:line; set confidence by "
    "how well-corroborated it is; do not guess.\n\n"
    "Respond with ONLY JSON. To investigate further:\n"
    '{"thought": "...", "actions": [{"search": "terms"}, {"read_file": "path"}, '
    '{"list_dir": "dir"}]}\n'
    "When you have a proven root cause:\n"
    '{"root_cause": "2-4 sentences citing file:line", "suspected_files": ["path"], '
    '"confidence": 0.0-1.0, "done": true}'
)

DOC_GROUNDING_SYSTEM = (
    "You validate a bug diagnosis against authoritative external documentation."
)

PROBE_SYSTEM = (
    "You decide whether validating a bug diagnosis requires inspecting a "
    "third-party Python library's type definitions, and if so which pip "
    "package defines them. Reply with ONLY one JSON object, no prose."
)


def build_probe_prompt(
    title: str, summary: str, suspected_files: list[str], issue_snippet: str
) -> str:
    return (
        f"Failure: {title}\n"
        f"Current diagnosis: {summary}\n"
        f"Suspected repository files: {', '.join(suspected_files) or '(none)'}\n\n"
        f"Issue excerpt:\n{issue_snippet[:1500]}\n\n"
        "Does validating this diagnosis depend on the exact shape of a "
        "third-party (pip-installable) library's objects — response types, "
        "field names, enums? Answer with ONLY this JSON:\n"
        '{"relevant": <bool>, "package": "<pip distribution defining those '
        'types, e.g. google-genai>", "version_hint": "<that package\'s version '
        'if the issue states one, else null>", "keywords": ["2-4 class/field '
        'names to locate, e.g. usage_metadata"]}'
    )


def build_grounding_prompt(title: str, diagnosis: str, schema_block: str, code_context: str) -> str:
    # With SDK definitions in hand, force an explicit field-name diff:
    # #19293 showed the model can hold the rename evidence in its prompt
    # and still anchor on its prior story unless told to compare names.
    crosscheck = (
        "FIRST, cross-check field names: list each attribute or key the "
        "suspect code reads from the third-party library's objects, and "
        "check each one against the SDK type definitions above. If a "
        "field the code reads is missing there but the definitions carry "
        "a similarly-named field (a rename, e.g. old vs new API "
        "versions), that mismatch is the most likely root cause — name "
        "both fields explicitly in your summary.\n\n"
        if schema_block
        else ""
    )
    prompt = (
        f"Failure: {title}\n"
        f"Current diagnosis: {diagnosis}\n\n"
        + (f"{schema_block}\n\n" if schema_block else "")
        + f"Code context:\n{code_context or '(none)'}\n\n"
        + crosscheck
        + "Validate this diagnosis against authoritative external documentation. "
        "Use web_search ONLY if the root cause depends on third-party API/library "
        "behavior (e.g. a renamed field or changed return shape in a dependency). "
        "Return ONLY the corrected root-cause summary in 2-4 sentences; if the "
        "original was correct, restate it concisely."
    )
    return prompt
