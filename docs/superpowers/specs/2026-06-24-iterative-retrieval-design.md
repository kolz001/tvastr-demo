# Design Spec: Iterative (hypothesis-driven) retrieval

**Date:** 2026-06-24
**Branch:** `feature/iterative-retrieval` (off `main`)
**Status:** Approved design — ready for implementation plan

## Problem

The agent forms the right hypothesis but can't act on it, because retrieval is
single-shot. Observed on llama_index issue **#19293** ("No Input/Output Token
count for Gemini 2.5 models"), benchmarked against maintainer PR **#21897**:

- `investigate` ran **one** keyword search on the issue title → the only match
  was an example notebook
  (`docs/examples/.../multimodal_rag_guardrail_gemini_llmguard.ipynb`).
- `reason_root_cause` *correctly* concluded "the bug is **not** in this notebook
  — it's in the gemini integration's token-usage parsing" (it even named
  `candidates_token_count`).
- But `generate_fix` could only edit the one file `investigate` had retrieved,
  so the agent "fixed" the notebook. The maintainer PR patched
  `core/callbacks/token_counting.py` + the google-genai integration.
- Benchmark verdict: **divergent**, `same_root_cause=false`, `files_both=[]`,
  judge confidence **0.92**.

Root cause of the miss: **retrieval is single-pass and never chases the
hypothesis the reasoning step produces.** A human reads the issue, hypothesizes
the gemini token parser, then *goes and opens that file*.

## Goal & success criterion

Let the agent iterate: reason → "I need file/area X" → fetch it → reason again,
within a bounded loop, so it reaches the **right code** before generating a fix.

**Definition of success (measurable via the existing PR-benchmark):** on issues
that currently grade `divergent` due to wrong-file retrieval, the agent-vs-PR
verdict should move toward `match`/`partial`. Issue #19293 is the canonical
manual spot-check post-build.

## Scope

**In scope:** iterative, hypothesis-driven *retrieval* only.

**Explicitly out of scope (future specs):**
- Documentation grounding (fetch external API docs/changelogs and compare to
  code) — the natural follow-up; this spec is its prerequisite (you need the
  right code before docs have anything to compare against).
- Confidence-gate changes (e.g. LLM self-reported confidence). The gate is left
  untouched here; the benchmark will tell us later whether it needs work.
- New retrieval primitives (directory/tree browse, filename/symbol search). We
  reuse the two existing tools and log path-misses so we can tell if navigation
  is ever actually needed.

## Approach

Express the loop as a **LangGraph conditional-edge loop** between
`reason_root_cause` and a new `expand_context` node. The first retrieval pass
(`investigate`) is unchanged; the new behavior is isolated to one new node, one
routing function, and a directive-output extension of the reasoning step.

The LLM **proposes targets**; a bounded loop executes them (chosen over a
free-form ReAct tool-agent for observability, bounded cost, and PII-safety, and
over heuristic import-following which wouldn't have helped here).

### Topology

```
Today:    investigate → reason_root_cause → (confidence_gate) → generate_fix → …

Proposed: investigate → reason_root_cause ──(_should_expand?)──┐
                            ▲                                   ├ "expand" → expand_context ┐
                            └───────────────────────────────────┘                          │
                            └──────────────────────(loop back)──────────────────────────────┘
                                                    │
                                        "done" → (confidence_gate) → generate_fix → …
```

- `investigate` — unchanged: stack-trace files, or one keyword search.
- `reason_root_cause` — now also emits retrieval directives (below).
- `_should_expand` — routes `"expand"` iff `need_more_context` **and**
  `retrieval_iterations < 2` **and** ≥1 *new* target; else `"gate"`.
- `expand_context` — executes targets, merges new files, loops back to
  `reason_root_cause`.
- `confidence_gate` — unchanged; fires once after the loop converges.

## Components

### State additions (`AgentState`, `total=False`)
- `retrieval_iterations: int` — count of `expand_context` rounds (0 initially).
- `retrieved_paths: set[str]` — every path fetched **and** every query issued,
  for cross-round dedup.
- `need_more_context: bool` — written by `reason_root_cause`.
- `next_targets: dict` — `{"queries": list[str], "paths": list[str]}`.
- `code_files` / `code_context` — already exist; now **accumulate** across rounds.

### `reason_root_cause` (modified)
Prompt gains a directive schema; the model returns its usual root-cause analysis
**plus**:
```json
{ "need_more_context": true,
  "next_targets": { "queries": ["candidates_token_count"],
                    "paths": ["llama-index-integrations/.../google_genai/utils.py"] } }
```
Parsed with the shared `tvastr.analysis._jsonutil.extract_json`. Absent or
unparseable directives → `need_more_context = false` (fail-safe: never loop on
garbage; preserves today's single-pass behavior and mock-mode runs).

### `expand_context` (new node)
For each target: `search_codebase` on new queries, `retrieve_code_files` on new
paths; **skip** anything already in `retrieved_paths`; cap accumulated files at
`_MAX_CONTEXT_FILES` (12). Merge into `code_files`/`code_context`, update
`retrieved_paths`, increment `retrieval_iterations`. Emits a `tool.call` per
retrieval and an `agent.node.start/end` carrying `files_added` and
`missing_paths` (proposed paths that returned nothing — logged so we learn
whether a navigation primitive is needed).

### `_should_expand` (new routing fn)
`"expand"` iff `need_more_context and retrieval_iterations < 2 and <≥1 new
target>`; else `"gate"`.

Net new surface: one state extension, one modified node, one new node, one
routing function. No new tools, no new external dependencies.

## Data flow (on issue #19293)

```
investigate:     search title → notebook.ipynb            (retrieved_paths={notebook}, iter=0)
reason pass 0:   "bug is in the gemini token parser, not the notebook";
                 need_more_context=true; next_targets={queries:[candidates_token_count], paths:[…utils.py]}
 _should_expand → expand
expand_context:  search "candidates_token_count" → token_counting.py, google_genai/utils.py
                 merge real source                         (iter=1)
reason pass 1:   root cause grounded in the parser; need_more_context=false
 _should_expand → gate → generate_fix (now editing the right files)
```

## Termination (four independent stops — the loop provably halts)

1. **LLM done:** `need_more_context=false`.
2. **Hard cap:** `retrieval_iterations >= 2` (≤2 extra reasoning calls; ≤3 passes).
3. **No new targets:** all proposed queries/paths already in `retrieved_paths`.
4. **Fail-safe:** unparseable/absent directives ⇒ treated as done.

Worst-case cost: **+2 reasoning calls + their retrievals**. Easy cases cost
exactly what they do today (model says done after pass 0).

## Error handling (no new run-aborting failure mode)

- **Path 404 / missing file:** `retrieve_code_files` returns `{}`; recorded in
  `missing_paths`, round continues.
- **Search error:** `search_codebase` already degrades to `[]`.
- **Unparseable directives:** single pass (= today).
- **Zero new files in a round:** stop #3 → gate.
- **`expand_context` raises:** wrapped in try/except → log, route to gate with
  current `code_files`; the run still reaches a verdict.
- **PII boundary unchanged:** new searches/file contents flow through the same
  `router.run` redaction path; the loop adds no cloud entry point that bypasses
  redaction.

## Testing (TDD, offline via the mock-LLM seam)

**`expand_context` unit:** executes queries+paths & merges; dedup skips
already-fetched targets; accumulation caps at `_MAX_CONTEXT_FILES`; a missing
path lands in `missing_paths` and the round still succeeds.

**Loop (graph-level):**
- *Converges* — mock reasons `need_more=true`(+new targets) then `false` → one
  expansion, `code_files` grew, ends at gate. (Regression test for #19293.)
- *Hard cap* — mock always `need_more=true` → stops after 2 expansions.
- *No-progress* — mock re-requests fetched paths → stop #3.
- *Fail-safe / back-compat* — mock emits no directives → single pass to gate.

**Degradation:** `expand_context` raising → routed to gate, run still verdicts.

**Regression guard:** existing `investigate`/agent tests stay green; `_should_expand`
routes correctly when state lacks the new keys.

**Live bar:** spot-check #19293 (and a few currently-`divergent` issues) — verdict
should move toward `match`/`partial`.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/agent/graph.py` | `reason_root_cause` emits directives; new `expand_context` node + `_should_expand` edge; loop wiring |
| `src/tvastr/agent/state.py` | new state keys |
| `src/tvastr/agent/tools/code_retrieval.py` | (reuse; possibly a small helper for merge/dedup) |
| `tests/test_agent_investigate.py` / new `tests/test_agent_expand.py` | loop + unit + degradation tests |

Module docstring flow diagram in `graph.py` to be updated to include the loop.
