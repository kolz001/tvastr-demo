# Design Spec: Agentic investigator (hybrid ReAct diagnosis core)

**Date:** 2026-06-25
**Branch:** `feature/query-formulation` (off `main`) — scope grew from query-formulation into the investigator
**Status:** Approved design — pending user review of this written spec

## Problem

The agent's diagnosis is a rigid pipeline (`investigate` single keyword search →
`reason_root_cause` → `expand_context` loop), and each recent feature has
hand-coded one behavior a good debugging agent would do on its own. On
llama_index #15743 ("WeaviateVectorStore uses broken filter") the pipeline:
- seeded its search from an **incidental `AioRpcError`** in the body, not the
  real symptom (`no such prop with name 'id'`) → searched the wrong topic;
- never read the relevant files *whole* or cross-referenced sibling code paths;
- the maintainer fix was a one-liner found by noticing `delete()` uses
  `Filter.by_id()` while `query()` uses `by_property("id")` — a consistency
  check the pipeline never performs.

A Claude-Code debugging session nailed it fast by: reading the full issue,
treating the reporter's guess as a hypothesis to verify, reading whole source
files, cross-referencing the store/query/delete paths, and grounding the fix in
evidence (no guessing). Those are emergent behaviors of a tool-using agent
following a debugging *method* — not stages to hand-wire.

## Goal & success criterion

Replace the rigid diagnosis core with a **bounded tool-using investigator
sub-agent** (inside the existing graph shell) that makes query-formulation and
cross-referencing emergent.

**Success:** re-run #15743 — the investigator reads the full issue, searches the
real subject, reads `weaviate/base.py` whole + the `utils.py` sibling,
cross-references `delete()` vs `query()`, produces the correct `by_id` root
cause, and `generate_fix` edits `weaviate/base.py` → the `benchmark.compared`
verdict moves off `divergent`.

## Scope

**In scope:** a `_investigate` node that runs a structured propose→execute loop
with `{search_code, read_file, list_dir}` + the full issue body + a
systematic-debugging method prompt; remove `reason_root_cause`/`expand_context`;
gate consumes the investigator's self-reported confidence; `list_dir` tool +
issue-body plumbing.

**Out of scope (explicit next steps):**
- **Investigator sandbox access for dynamic reproduction** — the investigator
  reasons *statically* (reads code, does not execute). Letting it run a
  reproducer mid-investigation (investigate → *reproduce* → hypothesize, the
  full debugging order) is the next frontier; deferred because it needs sandbox
  access inside the loop. Recorded here as the planned follow-up.
- Native Anthropic tool-use loop (we use the structured JSON loop; native
  tool-use is a later infra upgrade).
- `grep` action (read-whole + list_dir cover the #15743 cross-reference).

## Decisions (locked in brainstorming)

1. **Hybrid:** keep the graph shell (confidence gate, PII routing,
   `ground_root_cause`, `generate_fix`, `compare_to_pr`/benchmark, verify loop,
   event timeline); replace only the diagnosis core.
2. **Structured propose→execute loop** (not native tool-use) — bounded,
   observable, offline-testable, reuses the `expand_context`/`scripted_reasoning`
   patterns.
3. **Capabilities:** full issue body + `{search_code, read_file (whole),
   list_dir}`.
4. **Confidence:** the investigator self-reports confidence (replaces the
   `evidence_source` heuristic); gate threshold unchanged.

## Graph reshape

```
Before: investigate → reason_root_cause ⇄ expand_context → (gate) → ground → fix → compare → …
After:  investigate (agentic loop) → (confidence_gate) → ground_root_cause → generate_fix → compare_to_pr → … → notify
                                                  skip → notify
```
`reason_root_cause`, `expand_context`, `_should_expand`, `_after_reason` are
removed; iteration is internal to `_investigate`; the gate is a plain
conditional edge again (`{act: ground_root_cause, skip: notify}`). `ground_root_cause`,
`generate_fix`, `compare_to_pr`, verify, PII routing, events — unchanged.

## The investigator loop

`_investigate(state)` runs a bounded propose→execute loop.

**Start context:** full issue (title + body), the failure pattern, and
stack-trace files (existing `extract_stack_files` still seeds suspected files
when a traceback exists).

**Each round** the model receives the accumulated context + the method system
prompt and returns JSON — actions or finish:
- actions: `{"thought": "...", "actions": [{"search": "..."}, {"read_file": "..."}, {"list_dir": "..."}]}`
- finish: `{"root_cause": "...", "suspected_files": ["..."], "confidence": 0.0-1.0, "done": true}`

The node executes each action, merges results into context, emits a `tool.call`
per action, and loops.

**Method (system prompt) — the four disciplines from the Claude-Code review:**
1. **Root-cause-first:** do not finish with a `root_cause` until you've read the
   actual code that proves it; set `confidence` low if you haven't.
2. **Verify the reporter's hypothesis:** extract the real symptom AND any
   reporter-proposed cause; treat the latter as a hypothesis to confirm against
   the code, not as fact.
3. **Cross-reference for inconsistency:** compare related/sibling code paths
   (read / write / delete) and look for the mismatch.
4. **Cite evidence:** the `root_cause` must cite specific `file:line`; confidence
   reflects how well-corroborated it is — do not guess.

**Termination (bounded):** `done:true` → finish; hard cap
`_MAX_INVESTIGATE_ROUNDS = 4` → best-so-far; unparseable/no-new-actions →
finish (fail-safe).

**Output contract:** returns `root_cause` (`RootCause` with `summary`,
`suspected_files`, `confidence`, `reasoning`) + accumulated `code_files`/
`code_context` (deduped via `retrieved_paths`, capped at `_MAX_CONTEXT_FILES =
12`). Same keys `generate_fix`/`compare_to_pr` already consume.

## Components

| File | Change |
|------|--------|
| `agent/graph.py` | `_investigate` rewritten as the loop + method system prompt; `_parse_investigation` helper; remove `reason_root_cause`/`expand_context`/`_should_expand`/`_after_reason`; gate becomes a plain conditional edge. |
| `agent/tools/code_retrieval.py` | `list_dir(ctx, path) -> list[str]` wrapper. |
| `integrations/github.py` (CodeHost + Mock + real) | `list_dir(path) -> list[str]` (real: GitHub contents API; mock: canned). |
| `agent/state.py` | `issue_body: str \| None`. |
| `pipeline.py` + `api/routes/run.py` | thread `issue_body` into the agent seed (mirrors `pr_ref`/`pr_diff`). |
| `tests/` | new `tests/test_agent_investigate.py`; remove `tests/test_agent_reasoning.py` + `tests/test_agent_expand.py` (their nodes are gone). |

## Confidence / gate

The investigator self-reports `confidence`; `_confidence_gate` compares it to
`ctx.min_confidence` (0.5, unchanged). Replaces the `evidence_source` table — a
model that read the real code calibrates better than a fixed lookup.

## PII boundary

Every round's model call goes through `router.run` (cloud → `redact()`); the
accumulated context (issue body + every file/dir result) is part of that prompt,
so it's redacted on every round. No new bypass.

## Error handling (never crashes the run)

- Tool actions degrade (`search→[]`, `read→{}`, `list→[]`), logged, loop continues.
- Unparseable/cap → best-so-far root cause; if none, a low-confidence
  "insufficient evidence" `RootCause` → gate routes `skip` (escalate). Node
  always returns valid state.
- Missing `issue_body` → fall back to `pattern.title`/`representative_message`.

## Observability

Reuse existing events: per round, the model's `thought` + a `tool.call` per
action; node closes with `agent.node.end` (`rounds_used`, `files_read`,
`confidence`, `summary`). No new event types.

## Testing (offline — scripted router sequence + fake code_host with list_dir)

- Loop converges (actions round → done round) → root_cause + code_files set.
- Hard cap → stops at `_MAX_INVESTIGATE_ROUNDS`, best-so-far.
- Action dispatch: search/read_file/list_dir invoked, merged, dedup + cap.
- Unparseable → finish, low-confidence root cause (gate skips).
- Full issue body in round-1 prompt; method phrases in system prompt.
- `list_dir` tool + wrapper; mock canned siblings.
- Graph: removed-node references gone; `investigate → gate → ground` wired;
  `test_agent_reasoning.py`/`test_agent_expand.py` removed; gate/ground/fix/
  compare/verify tests stay green.
- Live metric: #15743 → correct `by_id` diagnosis, fix on `weaviate/base.py`,
  benchmark off `divergent`.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/agent/graph.py` | investigator loop + method prompt + node removal + gate edge |
| `src/tvastr/agent/tools/code_retrieval.py` | `list_dir` wrapper |
| `src/tvastr/integrations/github.py` | `list_dir` on CodeHost/mock/real |
| `src/tvastr/agent/state.py` | `issue_body` |
| `src/tvastr/pipeline.py`, `src/tvastr/api/routes/run.py` | issue_body plumbing |
| `tests/test_agent_investigate.py` (new); remove `test_agent_reasoning.py`, `test_agent_expand.py` |
