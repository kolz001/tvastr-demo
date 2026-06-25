# Design Spec: Force the web search in doc-grounding

**Date:** 2026-06-24
**Branch:** to be created off `main` at implementation time
**Status:** Approved design — ready for implementation plan

## Problem

The `ground_root_cause` node offers Anthropic's `web_search` tool and lets Claude
*decide* whether to use it. That decision is nondeterministic: on llama_index
#19293 one run searched and corrected a sub-theory, a later run **declined**
(`doc.grounded searched=false, sources=0`) and the diagnosis went unaided. For a
bug whose fix depends on a third-party API contract, leaving the search to
Claude's per-call discretion means grounding sometimes silently no-ops — so
"doc-grounding enabled" doesn't reliably mean "the diagnosis was grounded."

## Goal & success criterion

When grounding runs, make it actually consult docs — deterministically.

**Success:** on #19293, `doc.grounded` reliably reports `searched: true` with
sources across repeated runs (the search fires every time instead of
coin-flipping). Out of scope: the separate fix-precision issue (the agent can
still produce a shallow fix even with a grounded diagnosis) — that's a future
lever, not this one.

## Scope

**In scope:** force the `web_search` tool via `tool_choice` in
`ClaudeClient.complete` when `web_search=True`, with a fail-safe fallback.

**Out of scope:** conditional/heuristic forcing; changing the grounding node,
prompt, gating, or the `searched` proxy; fix quality.

## Decisions (locked in brainstorming)

1. **Always force when grounding runs.** `web_search=True` is used only by the
   grounding node, and grounding only runs on the act path with the flag on in
   live mode (already gated + opt-in) — so forcing every time is deterministic
   and the cost (one search per acting run) is negligible. No heuristic.
2. **Mechanism:** add `tool_choice={"type": "tool", "name": "web_search"}` to the
   request when `web_search` is on. (Equivalent to `{"type": "any"}` here since
   only one tool is offered; the explicit named form is used.)
3. **Fail-safe fallback:** if a forced request raises (the API rejects
   `tool_choice` for the server-side tool), retry once **without** `tool_choice`
   (the current offered behavior) so grounding still runs. The node's existing
   `try/except → doc.skipped` is the final backstop.

## Component (single file: `src/tvastr/llm/claude.py`)

`ClaudeClient.complete(prompt, *, system=None, web_search=False)`:
- When `web_search`: set `kwargs["tools"] = [_WEB_SEARCH_TOOL]` (existing) **and**
  `kwargs["tool_choice"] = {"type": "tool", "name": "web_search"}` (new).
- Wrap the `client.messages.create(**kwargs)` call so that, if it raises while
  `tool_choice` is set, it retries once with `tool_choice` removed (tools still
  offered). Citation/text extraction is unchanged.
- `MockClaudeClient.complete` is unchanged (ignores `web_search`).

No new params, no new flags, no changes to the router, the grounding node, state,
config, or the UI.

## Edge cases / error handling

- **Forced search returns no citations:** the search ran but `sources` is empty →
  `doc.grounded {searched: false}` via the existing `bool(sources)` proxy. The
  diagnosis still benefits; the under-report is the known deferred Minor.
- **API rejects `tool_choice` for the server tool:** the one-retry fallback runs
  the offered (non-forced) request, so grounding degrades to today's behavior
  rather than hard-failing.
- **Mock mode / no key:** unchanged — the mock ignores `web_search`; the node is
  gated off.

## Testing (offline, mocked `anthropic.Anthropic`)

- **Forces the tool:** `complete(web_search=True)` → captured `messages.create`
  kwargs include `tool_choice == {"type": "tool", "name": "web_search"}` and
  `tools == [_WEB_SEARCH_TOOL]`.
- **No forcing when off:** `complete(...)` without `web_search` → captured kwargs
  has neither `tools` nor `tool_choice`.
- **Fallback retry:** a fake whose `messages.create` raises when `tool_choice` is
  present and succeeds when absent → `complete(web_search=True)` returns the
  successful response, and the successful call's kwargs have no `tool_choice`.
- **Citations unchanged:** the forced-path happy test (text block + citation)
  still yields `sources`.
- **Regression:** the rest of `test_llm_web_search.py` + agent-grounding tests
  stay green.
- **Live metric:** re-run #19293 a few times — `doc.grounded` reliably
  `searched: true` with sources.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/llm/claude.py` | force `tool_choice` when `web_search`; one-retry fallback |
| `tests/test_llm_web_search.py` | force / no-force / fallback tests |
