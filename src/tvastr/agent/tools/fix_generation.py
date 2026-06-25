"""Tool: generate a concrete fix proposal via the cloud model.

Asks Claude for a structured JSON response describing *surgical* search/replace
operations against the retrieved source files, then validates each operation
(the search string must exist and be unique in the file) and applies it. The
result is a :class:`FixProposal` whose ``FileChange``s carry the *actual*
post-fix file contents — what a real PR would commit.

If the model's response can't be parsed or no operations validate, falls back to
emitting the prose explanation as a placeholder (today's behavior). The pipeline
keeps running and the audit log records the degradation.
"""

from __future__ import annotations

import difflib
import json
import re

from tvastr.agent.context import AgentContext
from tvastr.agent.tools.code_retrieval import format_code_for_prompt
from tvastr.domain import FailurePattern, FileChange, FixProposal, RootCause, RoutingDecision
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You are a senior software engineer producing minimal, correct code fixes as JSON. "
    "Respond with a single JSON object — no prose, no markdown fences, no commentary."
)

_PROMPT_SCHEMA_HINT = """\
Produce a fix as JSON with this exact schema:
{
  "summary": "1-3 sentences describing the fix and why it addresses the root cause",
  "changes": [
    {
      "path": "<one of the EDITABLE file paths shown above>",
      "search": "<exact verbatim substring from that file, including indentation and newlines>",
      "replace": "<the substring that should appear in its place>",
      "rationale": "<one sentence on why this change>"
    }
  ],
  "test_plan": "<2-4 sentences on how to verify the fix>"
}

Constraints:
- The "search" string MUST appear EXACTLY ONCE in the file shown above. Include
  enough surrounding lines (3+) to make it unique.
- Whitespace and indentation in "search" must match the file byte-for-byte.
- Keep edits surgical — modify only what's needed to address the root cause.
- If you cannot produce a high-confidence fix from the context provided, return
  {"summary": "...", "changes": [], "test_plan": "..."} and explain why in
  "summary"."""

# Pulls the first balanced {...} block from a response, tolerating Claude wrapping
# the JSON in ```json fences or prepending a sentence despite the system prompt.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class _ParsedFix:
    __slots__ = ("changes", "summary", "test_plan")

    def __init__(self, summary: str, changes: list[dict], test_plan: str) -> None:
        self.summary = summary
        self.changes = changes
        self.test_plan = test_plan


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of ``text``; return ``None`` if none parses."""
    # Strip common code fences first so the greedy {...} match doesn't pick up backticks.
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.DOTALL).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _parse_response(text: str) -> _ParsedFix | None:
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        return None
    changes = obj.get("changes")
    if not isinstance(changes, list):
        return None
    valid_changes: list[dict] = []
    for c in changes:
        if not isinstance(c, dict):
            continue
        if not all(isinstance(c.get(k), str) for k in ("path", "search", "replace")):
            continue
        valid_changes.append(c)
    return _ParsedFix(
        summary=str(obj.get("summary", "")).strip(),
        changes=valid_changes,
        test_plan=str(obj.get("test_plan", "")).strip(),
    )


def _apply_change(file_content: str, search: str, replace: str) -> tuple[str | None, str]:
    """Apply a single search/replace; return ``(new_content, error)``.

    ``new_content`` is ``None`` and ``error`` is non-empty on failure.
    """
    if not search:
        return None, "search string was empty"
    count = file_content.count(search)
    if count == 0:
        return None, "search string not found in file"
    if count > 1:
        return None, f"search string appears {count} times — must be unique (add more context)"
    return file_content.replace(search, replace, 1), ""


def _is_doc_example(path: str) -> bool:
    """A documentation/example artifact — read-only context, never an edit target.

    True for Jupyter notebooks and anything under a ``docs/`` or ``examples/``
    directory. Maintainers fix library source, not example notebooks.
    """
    if path.endswith(".ipynb"):
        return True
    segments = path.split("/")
    return "docs" in segments or "examples" in segments


def _editable_files(code_files: dict[str, str]) -> dict[str, str]:
    """The subset of retrieved files that may be EDITED — source, not docs/examples.

    Allow-as-fallback: when no source files were retrieved (everything is a
    doc/example), returns all files so a genuinely notebook-only bug stays fixable.
    """
    editable = {p: c for p, c in code_files.items() if not _is_doc_example(p)}
    return editable or dict(code_files)


def _build_real_changes(
    parsed: _ParsedFix,
    code_files: dict[str, str],
    allowed_paths: dict[str, str] | None = None,
) -> tuple[list[FileChange], list[str]]:
    """Validate + apply each parsed change; return ``(file_changes, errors)``.

    Multiple operations against the same file are applied sequentially so a
    response can express several hunks per file.
    """
    working: dict[str, str] = dict(code_files)
    rationales: dict[str, list[str]] = {}
    errors: list[str] = []

    allowed = code_files if allowed_paths is None else allowed_paths
    for c in parsed.changes:
        path = c["path"]
        if path not in working:
            errors.append(f"{path}: not in retrieved file set")
            continue
        if path not in allowed:
            errors.append(f"{path}: not editable (documentation/example — read-only)")
            continue
        new_content, err = _apply_change(working[path], c["search"], c["replace"])
        if new_content is None:
            errors.append(f"{path}: {err}")
            continue
        working[path] = new_content
        rationales.setdefault(path, []).append(c.get("rationale", "").strip() or "edit applied")

    file_changes: list[FileChange] = []
    for path, new_content in working.items():
        original = code_files[path]
        if new_content == original:
            continue  # untouched file — no FileChange
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
            )
        )
        file_changes.append(
            FileChange(
                path=path,
                original_snippet=None,
                patched_content=new_content,
                rationale="; ".join(rationales.get(path, [])),
                diff=diff,
            )
        )
    return file_changes, errors


def _fallback_changes(
    pattern: FailurePattern, response_text: str, code_files: dict[str, str]
) -> list[FileChange]:
    """When parsing/validation fails: emit a placeholder so downstream nodes still run.

    Mirrors the pre-upgrade behavior so the pipeline degrades gracefully instead
    of crashing on a malformed model response.
    """
    target = next(iter(code_files), "UNKNOWN.py")
    log.warning("tool.fix_generation.fallback", pattern=pattern.fingerprint, target=target)
    return [
        FileChange(
            path=target,
            patched_content=(
                f"# tvastr: fix proposal could not be applied automatically.\n"
                f"# Pattern: {pattern.title}\n"
                f"# Model output:\n# {response_text.strip()[:500]}\n"
            ),
            rationale="model output could not be parsed/validated; placeholder emitted",
        )
    ]


def generate_fix(
    ctx: AgentContext,
    pattern: FailurePattern,
    root_cause: RootCause,
    code_files: dict[str, str],
) -> tuple[FixProposal, RoutingDecision]:
    editable = _editable_files(code_files)
    context_only = {p: c for p, c in code_files.items() if p not in editable}
    editable_blob = format_code_for_prompt(editable) or "(no source files retrieved)"
    prompt = (
        f"Failure: {pattern.title}\n"
        f"Representative message: {pattern.representative_message}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"EDITABLE source files (your fix MUST target one of these):\n{editable_blob}\n\n"
    )
    if context_only:
        prompt += (
            "READ-ONLY context (do NOT edit — examples/docs):\n"
            f"{format_code_for_prompt(context_only)}\n\n"
        )
    prompt += _PROMPT_SCHEMA_HINT
    response, decision = ctx.router.run(
        TaskType.FIX_GENERATION, prompt, sensitivity=pattern.sensitivity, system=_SYSTEM
    )

    parsed = _parse_response(response.text)
    if parsed is None:
        changes = _fallback_changes(pattern, response.text, code_files)
        summary = response.text.strip()[:500]
        test_plan = "Add a regression test reproducing the failure; assert it no longer occurs."
    else:
        changes, errors = _build_real_changes(parsed, code_files, editable)
        if not changes:
            log.warning(
                "tool.fix_generation.no_valid_changes",
                pattern=pattern.fingerprint,
                errors=errors,
                proposed=len(parsed.changes),
            )
            changes = _fallback_changes(pattern, response.text, code_files)
            summary = parsed.summary or response.text.strip()[:500]
        else:
            log.info(
                "tool.fix_generation.applied",
                pattern=pattern.fingerprint,
                files=[c.path for c in changes],
                rejected=len(errors),
            )
            summary = parsed.summary or "Fix proposal."
        test_plan = parsed.test_plan or (
            "Add a regression test reproducing the failure; assert it no longer occurs."
        )

    fix = FixProposal(pattern_id=pattern.id, summary=summary, changes=changes, test_plan=test_plan)
    return fix, decision
