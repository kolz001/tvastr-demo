# Fix-target Restriction (don't fix example notebooks) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `generate_fix` from editing example/documentation files when real source is available, deterministically.

**Architecture:** Inside the `generate_fix` tool, classify retrieved files into editable source vs read-only docs/examples. The prompt shows both but instructs edits only to source; `_build_real_changes` rejects any change targeting a non-editable path (when source exists). Allow-as-fallback: when only docs/examples were retrieved, everything stays editable.

**Tech Stack:** Python 3.12; the existing `generate_fix` tool + its test harness (`_ScriptedLLM` + `build_router` with `router.cloud` swapped).

## Global Constraints

- A path is documentation/example (read-only, not an edit target) iff it ends in `.ipynb` OR has a `docs` or `examples` **path segment** (split on `/`) — segment match, not substring (so `src/examples_helper.py` is source).
- **Allow-as-fallback:** the restriction only bites when ≥1 editable (source) file is in the retrieved set. If every retrieved file is a doc/example, all remain editable.
- **Belt-and-suspenders:** the prompt steers the LLM (editable vs read-only sections); `_build_real_changes` enforces (rejects non-editable targets via the existing `errors` path).
- **No new failure mode:** a rejected non-editable change routes into the existing `errors`/"no valid changes → prose fallback" machinery; the run never crashes.
- **No new flag** — always-on correctness fix. No graph/state/config changes.
- Backward compatible: `_build_real_changes`'s new `allowed_paths` param defaults to `None` (= all retrieved files editable) so existing callers/tests are unaffected.
- MANDATORY before committing: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/agent/tools/fix_generation.py` (modify) | `_is_doc_example`, `_editable_files`, prompt split in `generate_fix`, `allowed_paths` rejection in `_build_real_changes`, schema-hint wording |
| `tests/test_fix_generation.py` (modify) | helper units + restriction/fallback/prompt-split tests |

---

## Task 1: Restrict fix targets to source files

**Files:**
- Modify: `src/tvastr/agent/tools/fix_generation.py`
- Test: `tests/test_fix_generation.py`

**Interfaces:**
- Produces:
  - `_is_doc_example(path: str) -> bool`
  - `_editable_files(code_files: dict[str, str]) -> dict[str, str]` (source subset; full set if no source)
  - `_build_real_changes(parsed, code_files, allowed_paths=None)` — rejects changes whose path ∉ `allowed_paths` (when given); `None` = all retrieved files allowed.
  - `generate_fix` unchanged signature; internally splits editable vs read-only context and passes editable to `_build_real_changes`.

- [ ] **Step 1: Write the failing helper tests**

Append to `tests/test_fix_generation.py`:

```python
# --- fix-target restriction ------------------------------------------------

from tvastr.agent.tools.fix_generation import _editable_files, _is_doc_example


def test_is_doc_example_flags_notebooks_and_docs():
    assert _is_doc_example("docs/examples/x.ipynb") is True
    assert _is_doc_example("foo/bar.ipynb") is True          # any notebook
    assert _is_doc_example("docs/guide.md") is True          # docs segment
    assert _is_doc_example("examples/demo.py") is True       # examples segment
    assert _is_doc_example("llama-index-core/llama_index/core/callbacks/token_counting.py") is False
    assert _is_doc_example("src/examples_helper.py") is False  # substring, not a segment


def test_editable_files_returns_source_only_when_present():
    files = {"docs/examples/n.ipynb": "nb", "mod.py": "src"}
    assert _editable_files(files) == {"mod.py": "src"}


def test_editable_files_falls_back_to_all_when_no_source():
    files = {"docs/examples/n.ipynb": "nb", "docs/guide.md": "doc"}
    assert _editable_files(files) == files
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run: `uv run pytest tests/test_fix_generation.py -k "doc_example or editable_files" -v`
Expected: FAIL — `ImportError: cannot import name '_is_doc_example'`.

- [ ] **Step 3: Implement the two helpers**

In `src/tvastr/agent/tools/fix_generation.py`, add after `_apply_change` (before `_build_real_changes`):

```python
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
```

- [ ] **Step 4: Run helper tests to verify they pass**

Run: `uv run pytest tests/test_fix_generation.py -k "doc_example or editable_files" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write the failing behavior tests**

Append to `tests/test_fix_generation.py`:

```python
class _RecordingLLM:
    model = "claude-opus-4-7"
    target = "cloud"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_prompt: str | None = None

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        self.last_prompt = prompt
        return LLMResponse(
            text=self.response_text, model=self.model, target=self.target, mocked=True
        )


def test_build_real_changes_rejects_non_editable_path():
    from tvastr.agent.tools.fix_generation import _ParsedFix

    parsed = _ParsedFix(
        summary="s",
        changes=[{"path": "docs/examples/n.ipynb", "search": "x", "replace": "y", "rationale": "r"}],
        test_plan="t",
    )
    code_files = {"docs/examples/n.ipynb": "x\n", "mod.py": "z\n"}
    allowed = {"mod.py": "z\n"}  # notebook excluded
    changes, errors = _build_real_changes(parsed, code_files, allowed)
    assert changes == []
    assert any("not editable" in e for e in errors)


def test_generate_fix_edits_source_not_notebook():
    # LLM proposes BOTH a notebook edit (must be rejected) and a source edit (applied).
    response = (
        '{"summary": "fix", "test_plan": "t", "changes": ['
        '{"path": "docs/examples/n.ipynb", "search": "old", "replace": "new", "rationale": "r1"},'
        '{"path": "mod.py", "search": "return x", "replace": "return x or 0", "rationale": "r2"}'
        "]}"
    )
    ctx = _ctx_with_llm(_ScriptedLLM(response))
    pattern = _pattern()
    code_files = {"docs/examples/n.ipynb": "old\n", "mod.py": "return x\n"}
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), code_files)
    paths = [c.path for c in fix.changes]
    assert "mod.py" in paths
    assert "docs/examples/n.ipynb" not in paths


def test_generate_fix_allows_notebook_when_only_docs_retrieved():
    # Fallback: nothing but a notebook was retrieved -> the notebook is editable.
    response = (
        '{"summary": "fix", "test_plan": "t", "changes": ['
        '{"path": "docs/examples/n.ipynb", "search": "old", "replace": "new", "rationale": "r"}'
        "]}"
    )
    ctx = _ctx_with_llm(_ScriptedLLM(response))
    pattern = _pattern()
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), {"docs/examples/n.ipynb": "old\n"})
    assert [c.path for c in fix.changes] == ["docs/examples/n.ipynb"]


def test_generate_fix_prompt_labels_editable_vs_readonly():
    llm = _RecordingLLM('{"summary": "s", "test_plan": "t", "changes": []}')
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = llm
    ctx = AgentContext(router=router, code_host=MockGitHubClient(), notifier=build_notifier(settings))
    pattern = _pattern()
    generate_fix(ctx, pattern, _root_cause(pattern), {"docs/examples/n.ipynb": "nb\n", "mod.py": "src\n"})
    assert "EDITABLE source files" in llm.last_prompt
    assert "READ-ONLY context" in llm.last_prompt
    # the notebook appears in the read-only section, after the editable header
    assert llm.last_prompt.index("EDITABLE source files") < llm.last_prompt.index("docs/examples/n.ipynb")
    assert "READ-ONLY context" in llm.last_prompt[: llm.last_prompt.index("docs/examples/n.ipynb")]
```

- [ ] **Step 6: Run behavior tests to verify they fail**

Run: `uv run pytest tests/test_fix_generation.py -k "rejects_non_editable or edits_source_not_notebook or allows_notebook_when_only or prompt_labels" -v`
Expected: FAIL — `_build_real_changes` takes 2 args (TypeError on the 3rd), and `generate_fix` doesn't split the prompt yet.

- [ ] **Step 7: Add `allowed_paths` to `_build_real_changes`**

In `src/tvastr/agent/tools/fix_generation.py`, change the signature and the per-change loop. Replace:

```python
def _build_real_changes(
    parsed: _ParsedFix, code_files: dict[str, str]
) -> tuple[list[FileChange], list[str]]:
```

with:

```python
def _build_real_changes(
    parsed: _ParsedFix,
    code_files: dict[str, str],
    allowed_paths: dict[str, str] | None = None,
) -> tuple[list[FileChange], list[str]]:
```

and in the loop, after the `if path not in working:` block, add the editability check:

```python
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
```

(The `allowed = ...` line goes just before the `for c in parsed.changes:` loop, alongside the existing `working`/`rationales`/`errors` setup.)

- [ ] **Step 8: Split the prompt in `generate_fix` and pass the editable set**

In `generate_fix`, replace the prompt construction + the `_build_real_changes` call. Replace:

```python
    code_blob = format_code_for_prompt(code_files) or "(no source files retrieved)"
    prompt = (
        f"Failure: {pattern.title}\n"
        f"Representative message: {pattern.representative_message}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"Source files:\n{code_blob}\n\n"
        f"{_PROMPT_SCHEMA_HINT}"
    )
```

with:

```python
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
```

Then change the `_build_real_changes` call (in the `else` branch where `parsed is not None`):

```python
        changes, errors = _build_real_changes(parsed, code_files, editable)
```

- [ ] **Step 9: Update the schema-hint wording**

In `_PROMPT_SCHEMA_HINT`, change the `"path"` line from:

```
      "path": "<one of the file paths shown above>",
```

to:

```
      "path": "<one of the EDITABLE file paths shown above>",
```

- [ ] **Step 10: Run the behavior tests to verify they pass**

Run: `uv run pytest tests/test_fix_generation.py -v`
Expected: PASS (all — new + existing; existing `_build_real_changes(parsed, files)` 2-arg calls still work because `allowed_paths` defaults to `None`).

- [ ] **Step 11: Full suite + lint (regression)**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, "All checks passed!".

- [ ] **Step 12: Commit**

```bash
git add src/tvastr/agent/tools/fix_generation.py tests/test_fix_generation.py
git commit -m "fix(agent): restrict fix targets to source — example notebooks are read-only context"
```

---

## Final verification (after the task)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With the flags configured (live, doc-grounding on), run #19293 through `/app`. Confirm the agent now edits `token_counting.py`/`utils.py` (not the notebook), `files_both` overlaps PR #21897, and the `benchmark.compared` verdict moves `divergent → partial`/`match` — repeatably across 2-3 runs (the restriction removes the notebook-vs-source nondeterminism).

## Self-Review (completed by author)

- **Spec coverage:** `_is_doc_example` (.ipynb/docs/examples segment) — Step 3 + Step 1 tests; `_editable_files` source-only + allow-as-fallback — Step 3 + Step 1 tests; prompt split editable/read-only — Step 8 + prompt-labels test; `_build_real_changes` rejects non-editable — Step 7 + rejection test; allow-as-fallback end-to-end — Step 5 fallback test; no new flag / no graph-state-config change — confined to fix_generation.py; graceful degradation via existing errors path — Step 7 reuses `errors`. Live metric in Final verification.
- **Placeholder scan:** none — every step has complete code.
- **Type consistency:** `_is_doc_example(str)->bool`, `_editable_files(dict)->dict`, `_build_real_changes(parsed, code_files, allowed_paths=None)` used consistently; `generate_fix` passes `editable` (the `_editable_files` result) as `allowed_paths`; the rejection message substring "not editable" matches the test assertion.
