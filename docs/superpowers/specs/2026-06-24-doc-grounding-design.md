# Design Spec: Documentation grounding (`ground_root_cause`)

**Date:** 2026-06-24
**Branch:** to be created off the iterative-retrieval line (see Sequencing)
**Status:** Approved design — ready for implementation plan

## Problem

Iterative retrieval (shipped) gets the agent to the *right code*, but not the
*right knowledge*. Re-running llama_index issue #19293 (Gemini 2.5 token counts)
against maintainer PR #21897 now shows:

- `files_both = [token_counting.py]` (agent reaches the correct file — retrieval works),
- but verdict is still **divergent**: the agent added a generic `additional_kwargs`
  fallback + truthiness checks instead of handling the renamed key
  `candidates_token_count` → `response_token_count`. Per the benchmark rationale,
  it "misdiagnosed the root cause as a deprecated multi-modal path issue rather
  than the renamed token count field."

The agent has the file but doesn't *know* the Gemini 2.5 API renamed the field.
A human would read the SDK docs / changelog. The agent has no way to reach
anything outside the target GitHub repo.

## Goal & success criterion

Add a documentation-grounding step that lets the agent validate/correct its
root-cause diagnosis against authoritative external docs before generating the
fix — using Anthropic's built-in `web_search` tool.

**Success (measured via the PR-benchmark):** on issues whose fix depends on
external API/library behavior (like #19293), the verdict moves
`divergent → partial/match`, and the run's timeline shows a `doc.grounded` card
citing the source that revealed the correct behavior.

## Scope

**In scope:** a single `ground_root_cause` node that, on the act path, runs one
web-search-enabled Claude call to refine `root_cause` before `generate_fix`.

**Out of scope:** changing retrieval, the confidence gate, or `generate_fix`;
agent-controlled URL fetching/parsing (we use Anthropic's server-side search);
any non-Anthropic search provider.

## Decisions (locked in brainstorming)

1. **Source:** Anthropic's built-in server-side `web_search` tool (reuses
   `ANTHROPIC_API_KEY`; no new infra/keys; most "human-like").
2. **Placement:** a dedicated `ground_root_cause` node on the act path, between
   the confidence gate and `generate_fix` — it corrects the *diagnosis* stage,
   which is what missed and what `compare_to_pr` grades.
3. **Trigger:** the node always runs on the act path; the `web_search` tool is
   *offered* and Claude self-decides whether to invoke it (cost — the billed
   search — only happens when warranted). No LLM directive-schema change.
4. **Gating:** no-op (skip, `root_cause` unchanged) in mock mode, with no
   Anthropic key, or when `TVASTR_DOC_GROUNDING` is off (default off).

## Architecture

```
… reason_root_cause ⇄ expand_context
         │ gate: act                         │ gate: skip
         ▼                                    ▼
   ground_root_cause   ← NEW                 notify (unchanged)
         │
         ▼
   generate_fix → compare_to_pr → draft_pr → open_pr → notify
```

- The gate's `"act"` edge points to `ground_root_cause` (was `generate_fix`);
  `ground_root_cause → generate_fix`. The retrieval loop and `"skip"` path are
  untouched.
- The node makes **one** cloud call via `TaskType.DOC_GROUNDING` with the
  server-side `web_search` tool offered. Anthropic performs any searches and
  returns the final text **+ citations** in one response (server tool — no
  client-side tool loop).
- Output: the **grounded `root_cause`** (same `RootCause`, `summary`/`reasoning`
  corrected by what the docs revealed) overwrites `state["root_cause"]`; the
  citation URLs are recorded in `state["doc_sources"]` and on the event.

## Components

| Unit | Change |
|------|--------|
| `config.py` | `doc_grounding: bool = False` (env `TVASTR_DOC_GROUNDING`). |
| `llm/router.py` | `TaskType.DOC_GROUNDING = "doc_grounding"` (cloud); `run(...)` gains `web_search: bool = False`, passed to the client. |
| `llm/claude.py` | `ClaudeClient.complete(..., web_search=False)`: when true, adds `{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}` to the request and extracts final text + citation URLs from the response. `MockClaudeClient` accepts and ignores the param (no search, no citations). |
| `agent/state.py` | `doc_sources: list[str]`. |
| `agent/graph.py` | `_ground_root_cause` node; rewire gate `"act"` → `ground_root_cause` → `generate_fix`. |
| `events.py` | `"doc.grounded"`, `"doc.skipped"` event types (before `"error"`). |
| `api/templates/app.html` | `summarize()` cases for the two events (sources count / skip reason); generic card renderer already shows the payload. |

### The grounding call
`_ground_root_cause` builds a prompt from the (redacted) pattern, the current
`root_cause`, and the retrieved `code_context`, instructing Claude: *"Validate
this diagnosis against authoritative external documentation. Use web_search only
if the root cause depends on third-party API/library behavior. Return a
corrected root-cause summary; if the original was right, restate it."* It calls
`router.run(TaskType.DOC_GROUNDING, prompt, sensitivity=pattern.sensitivity,
web_search=True)`.

**PII:** `DOC_GROUNDING` is a cloud task, so the router redacts the prompt before
the call exactly like every other cloud task; search queries Claude derives come
from already-redacted content. No new bypass.

## Data flow (issue #19293)

```
gate: act → ground_root_cause:
  prompt = pattern + root_cause("deprecated multi-modal path…") + token_counting.py
  router.run(DOC_GROUNDING, prompt, web_search=True)
    → Claude judges the diagnosis hinges on Gemini 2.5 API behavior → web_search
    → finds: 2.5 reports usage under `response_token_count` (renamed)
    → corrected summary + citations
  root_cause.summary := grounded; doc_sources := [urls]; emit doc.grounded{searched:true,...}
  → generate_fix patches the renamed key → compare_to_pr grades it (target: partial/match)

No-correction path:
  - gated (mock / no key / flag off) OR the call raises → emit doc.skipped{reason},
    root_cause UNCHANGED
  - call succeeds but Claude declined to search (internal bug) → emit
    doc.grounded{searched:false, changed:false}, root_cause typically unchanged
  → generate_fix identical to today
```

**Event semantics (unambiguous):** `doc.skipped` means no diagnosis was produced
(gated before the call, or the call errored). `doc.grounded` means the call
returned; its payload carries `searched: bool` (did Claude invoke web_search),
`changed: bool` (did the summary change), and `sources: list[str]`.

## Termination / safety

- The node makes a single call and routes unconditionally to `generate_fix`; no
  loop, no recursion.
- **Never crashes the run:** the call is wrapped in try/except → on any failure
  (API error, search failure, unparseable response) it logs, emits
  `doc.skipped {reason: "grounding error: …"}`, returns `{}` (root_cause
  unchanged), and `generate_fix` proceeds with the pre-grounding diagnosis.
- Gates short-circuit before any API call (mock / no key / flag off →
  `doc.skipped {reason}`).

## Cost

- ≤1 cloud call per acting run when enabled; actual web searches happen only
  when Claude invokes the tool, capped at `max_uses=3`. Acting runs are
  confidence-gated, so this is infrequent.
- Default off (`TVASTR_DOC_GROUNDING`), consistent with `pii_local_model` and
  `auto_analyze_prs`; never bills a search unless explicitly enabled.

## Testing (TDD, all offline)

- **Back-compat / gating:** mock mode and flag-off → node emits `doc.skipped`,
  `root_cause` unchanged; existing suite stays green.
- **Grounding happy path:** a scripted router (mirroring `scripted_reasoning`)
  returns a corrected summary + sources for `DOC_GROUNDING` → node updates
  `root_cause.summary`, sets `doc_sources`, emits `doc.grounded {searched:true}`.
- **Degradation:** grounding call raises → `doc.skipped {error}`, `root_cause`
  unchanged, run still reaches a verdict.
- **Graph wiring:** act path is gate → `ground_root_cause` → `generate_fix`;
  skip path (→ notify) unchanged.
- **Cloud-client mechanism:** unit test that `ClaudeClient.complete(web_search=True)`
  adds the `web_search_20250305` tool to the request and extracts citations,
  via a mocked Anthropic client/transport — no real web call.
- **Events:** `doc.grounded`/`doc.skipped` present in `EventType`.
- **Live bar:** spot-check #19293 with the flag on — verdict should move
  `divergent → partial/match`, timeline shows a `doc.grounded` card with the
  renamed-field source.

## Sequencing / branch

This feature builds on the iterative-retrieval loop (it consumes the converged
`root_cause` + accumulated `code_context`). It should be based on the
iterative-retrieval line, not bare `main`. Exact branch base decided at
implementation time (after the user chooses how to land the stacked
iterative-retrieval / auto-analyze branches).

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/config.py` | `doc_grounding` flag |
| `src/tvastr/llm/router.py` | `DOC_GROUNDING` task + `web_search` param |
| `src/tvastr/llm/claude.py` | web_search tool wiring + citation extraction |
| `src/tvastr/agent/state.py` | `doc_sources` |
| `src/tvastr/agent/graph.py` | `_ground_root_cause` node + gate rewire |
| `src/tvastr/events.py` | `doc.grounded` / `doc.skipped` |
| `src/tvastr/api/templates/app.html` | summarize cases |
| `tests/` | client mechanism, node happy/skip/degradation, wiring, events |
