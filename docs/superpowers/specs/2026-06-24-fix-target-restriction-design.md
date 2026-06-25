# Design Spec: Fix-target restriction (don't fix example notebooks)

**Date:** 2026-06-24
**Branch:** to be created off `main` at implementation time
**Status:** Approved design — ready for implementation plan

## Problem

After iterative retrieval (gets the right code) and doc-grounding (corrects the
diagnosis), the agent *still* sometimes produces a divergent fix on llama_index
#19293 (Gemini token counts). The run log shows why: retrieval fetched the
library files (`token_counting.py`, `google_genai/utils.py`) AND grounding
searched + revised the diagnosis — but `generate_fix` chose to edit the **example
notebook** (`docs/examples/.../multimodal_rag_guardrail_gemini_llmguard.ipynb`)
instead of the library. The maintainer PR patches the library; the agent
"migrated the notebook." `files_both = []`, `files_ours_only = [notebook]` →
divergent. The behavior also oscillates run-to-run (an earlier run edited
`token_counting.py`).

Root cause: in `generate_fix`, the LLM freely picks which retrieved file to edit
(`"path": "<one of the file paths shown above>"`), and `_build_real_changes` only
checks the path is in the retrieved set — nothing steers it toward source over
example/doc artifacts. A maintainer fixes the library, not the example.

## Goal & success criterion

Stop the agent from editing example/documentation files when real source is
available, deterministically.

**Success (measured via the PR-benchmark):** on #19293 the agent edits the
library (`token_counting.py`/`utils.py`), `files_both` overlaps the PR, and the
verdict moves `divergent → partial`/`match` — repeatably across runs.

## Scope

**In scope:** a localized change to `src/tvastr/agent/tools/fix_generation.py`
(the `generate_fix` tool) that treats example/doc files as read-only context and
restricts *edits* to source files.

**Out of scope:** retrieval ranking (the failure is at fix-target, not
retrieval — the source was already retrieved); graph/state/config changes; any
new flag (this is a correctness fix, always on).

## Decisions (locked in brainstorming)

1. **Where:** fix-target restriction inside `generate_fix` (not retrieval).
2. **Doc/example rule:** a path is documentation/example (read-only, not an edit
   target) if it ends in `.ipynb` OR has a `docs` or `examples` path segment.
3. **Allow-as-fallback:** the restriction only bites when ≥1 *source* (editable)
   file is present in the retrieved set. If every retrieved file is a
   doc/example, all of them remain editable (no regression on notebook-only bugs).
4. **Belt-and-suspenders:** the prompt steers the LLM (lists editable vs
   read-only files); `_build_real_changes` enforces it (rejects non-editable
   edit targets when source exists), degrading via the existing error path.

## Components (all in `src/tvastr/agent/tools/fix_generation.py`)

- **`_is_doc_example(path: str) -> bool`** (new, pure): `True` if
  `path.endswith(".ipynb")` or `"docs"`/`"examples"` is a path segment
  (split on `/`). Segment match, not substring (so `src/examples_helper.py` is
  source).
- **`_editable_files(code_files: dict[str, str]) -> dict[str, str]`** (new):
  the subset where `not _is_doc_example(path)`; if that subset is empty, returns
  `code_files` unchanged (allow-as-fallback).
- **`generate_fix` changes:**
  - Compute `editable = _editable_files(code_files)` and
    `context_only = {p: c for p, c in code_files.items() if p not in editable}`.
  - Prompt renders two sections: "EDITABLE source files (your fix MUST target
    one of these)" = `editable`; "READ-ONLY context (do NOT edit — examples/docs)"
    = `context_only` (omitted when empty). Doc/example files stay visible as
    context so the LLM understands the symptom.
  - The schema hint's `"path"` line becomes "one of the EDITABLE file paths."
  - Pass the editable set into `_build_real_changes` as the allowed paths.
- **`_build_real_changes` change:** reject any change whose `path` is not in the
  editable (allowed) set — append to `errors` (same as the existing
  `path not in working` rejection) and skip applying it.

## Data flow (issue #19293)

```
generate_fix code_files = {notebook.ipynb, token_counting.py, google_genai/utils.py, base.py, test.py}
  editable = {token_counting.py, google_genai/utils.py, base.py, test.py}  (notebook excluded)
  prompt: EDITABLE = the 4 source files; READ-ONLY context = notebook.ipynb
  LLM edits token_counting.py / utils.py → applied → files_both overlaps PR → verdict improves
  (if LLM still targets notebook → rejected in _build_real_changes → existing no-valid-changes fallback)
```

## Edge cases / error handling (no new failure mode)

- **Docs/notebooks only retrieved:** `_editable_files` returns the full set →
  notebook editable again → identical to today.
- **LLM ignores the steer (source present):** all notebook changes rejected →
  no valid `FileChange`s → existing "fall back to prose placeholder" path; run
  never crashes; audit records the degradation.
- **Empty `code_files`:** `_editable_files({}) == {}` → fallback `{}`; existing
  empty-context handling applies.
- The only new branch ("reject non-editable path") routes into the existing
  `errors`/fallback machinery.

## Testing (TDD, offline)

- **`_is_doc_example`:** `docs/examples/x.ipynb`→True; `foo/bar.ipynb`→True;
  `docs/guide.md`→True; `examples/demo.py`→True; `.../token_counting.py`→False;
  `src/examples_helper.py`→False (segment, not substring).
- **`_editable_files`:** mixed set → source only; doc/example-only set → full set
  (fallback).
- **`generate_fix` steers + enforces:** retrieved set = notebook + source;
  scripted LLM proposes editing the notebook → `FixProposal` has no notebook
  change (rejected). (Regression test for #19293.)
- **Source edit applies:** scripted LLM edits the source file → `FileChange`
  produced normally.
- **Fallback:** notebook-only retrieved set; scripted LLM edits the notebook →
  edit IS applied.
- **Prompt split:** the prompt sent to the router labels the source file editable
  and the notebook read-only.
- **Regression guard:** existing `fix_generation`/agent tests stay green.
- **Live metric:** re-run #19293 — agent edits the library, `files_both`
  overlaps, verdict `divergent → partial`/`match`, deterministic across runs.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/agent/tools/fix_generation.py` | `_is_doc_example`, `_editable_files`, prompt split, `_build_real_changes` allowed-paths rejection |
| `tests/test_fix_generation.py` (or existing fix-tool test) | helper units + steer/enforce/fallback/prompt-split tests |
