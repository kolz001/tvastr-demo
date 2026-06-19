# Design: PR-aware triage + agent-vs-PR benchmark

**Date:** 2026-06-19
**Status:** Approved (brainstorming) — pending implementation plan
**Author:** Nikhil (with Claude)

## Summary

Every bug-labeled issue in the triage UI usually has a real pull request that
addresses it. Today tvastr ignores those PRs entirely (the existing "fixed in
PR" chip is only a regex over comment *text* — it never fetches or reads the
PR). This feature discovers each issue's PR, lets the LLM analyze it, and —
the headline — **grades tvastr's own generated fix against the maintainer's PR
as ground truth.** Every agent run self-reports whether it reached the same
root cause and touched the same code a human did.

The maintainer's PR becomes a **ground-truth oracle**: the comparison verdict
(`match` / `partial` / `divergent`) is an eval signal for the autonomous
agent, not a generic PR summary.

## Goals

- Discover the PR that addresses a given issue (cheap, no LLM).
- Let the LLM analyze a discovered PR (what it changes, whether it addresses
  the issue, the approach) — auto for the top 5 loaded issues, on-demand for
  the rest.
- Automatically compare tvastr's generated fix against the discovered PR at
  the end of each agent run, and stream a benchmark verdict into the live
  timeline.

## Non-goals

- A batch / aggregate benchmark dashboard across many issues (a possible later
  phase; out of scope here).
- Auto-applying or merging the upstream PR.
- Replacing the existing comment-based resolution chip (it stays; this is a
  separate, richer signal).

## Background / current state

- `GET /api/issues` lists top-N bug issues, ranked.
- `GET /api/resolution?repo&number` → cheap regex chip from comment text
  (`comments.py`). No PR fetch, no LLM.
- `/api/run` streams the agent pipeline over SSE (`routes/run.py`):
  `investigate → reason_root_cause → (confidence gate) → generate_fix →
  draft_pr → open_pr → notify`. Events are persisted to
  `data/runs/<run_id>.jsonl` and replayable.
- Hybrid router (`llm/router.py`) routes `TaskType`s to local or cloud; cloud
  = Claude. Reasoning tasks (root cause, fix gen, PR description) go cloud.
- The "Verify this fix" flow is the existing precedent for a post-run,
  LLM/sandbox-backed judgment streamed into the timeline with a verdict badge
  and a shown cost.

## Reality check (grounds the discovery approach)

GitHub timeline cross-reference linking is **sparse** for this repo —
maintainers rarely write "closes #N". But the **Search API reliably finds
candidate PRs**: `repo:run-llama/llama_index type:pr <issue#>`. For issue
#19293 it returns PR #21897 ("fix: add token counting support for Gemini 2.5",
open) plus a closed one. So discovery uses search + ranking, not formal links.

## Architecture

### Data flow

```
Issues load (top N)
   ├─► [cheap] PR discovery for ALL cards   GET /api/issue-pr?repo&number
   │       Search API → rank (open > merged > closed, then relevance)
   │       → PR ref; UI shows "PR #N ↗" chip
   ├─► [LLM] auto-analyze TOP 5 cards-with-PR   POST /api/pr-analysis
   │       spinner while pending; cards 6..N get a manual "Analyze PR" button
   └─► [LLM] "Apply fix (dry-run)" run   (existing /api/run, augmented)
           ... → generate_fix → compare_to_pr (NEW) → draft_pr → notify
           if a PR was discovered, agent self-grades and streams a
           benchmark verdict card (match / partial / divergent)
```

Two distinct LLM operations, separated by purpose:
- **PR analysis** — per card; describes the human PR. Needs no agent fix.
- **Fix comparison** — the benchmark; only inside a run; needs tvastr's fix.

### New package: `src/tvastr/analysis/`

**`pr_discovery.py`**
- `PullRequestRef` dataclass: `number, title, state ("open"|"closed"),
  merged: bool, url, changed_files: int`.
- `discover_pr(repo, number, *, token, use_mocks, transport=None)
  -> PullRequestRef | None`: GitHub Search API
  `repo:{repo} type:pr {number}`; rank candidates (prefer `open`, then
  `merged`, then `closed`; tie-break by most recently updated). Cached per
  `(repo, number)` with a TTL cache mirroring `comments.py` (10-min TTL,
  bounded size, lock-guarded). Returns `None` in mock mode or when no token.
- `PrDiff` dataclass: `files: list[PrFile]`, `truncated: bool`, where
  `PrFile = {filename, status, additions, deletions, patch}`.
- `fetch_pr_diff(repo, pr_number, *, token, transport=None) -> PrDiff`:
  `GET /repos/{repo}/pulls/{pr_number}/files` (paginated), **capped at 30
  files and ~1500 total patch lines**; sets `truncated=True` when it clips.

**`pr_analysis.py`**
- `PrAnalysis` dataclass: `addresses_issue: "yes"|"partial"|"no"`,
  `approach_summary: str`, `key_files: list[str]`, `root_cause: str`,
  `pr_number: int`, `pr_state: str`.
- `analyze_pr(issue_title, issue_body, pr_ref, pr_diff, router) ->
  (PrAnalysis, RoutingDecision)`: one cloud LLM call
  (`TaskType.PR_ANALYSIS`) over the issue text + capped PR diff. Mock mode
  returns a deterministic stub.

**`fix_comparison.py`**
- `FixComparison` dataclass:
  - `verdict: "match" | "partial" | "divergent"`
  - `same_root_cause: bool`
  - `files_both: list[str]`, `files_ours_only: list[str]`,
    `files_theirs_only: list[str]`
  - `equivalence: "functionally_equivalent" |
    "same_goal_different_approach" | "addresses_different_cause"`
  - `rationale: str` (2–4 sentences, cites specifics)
  - `confidence: float` (0.0 to 1.0)
- `compare_fix_to_pr(issue, our_fix: FixProposal, pr_ref, pr_diff, router)
  -> (FixComparison, RoutingDecision)`: one cloud LLM call
  (`TaskType.FIX_COMPARISON`). `files_*` sets are computed deterministically
  in code from the two change lists; the LLM judges root cause, equivalence,
  verdict, and rationale. Mock mode returns a deterministic stub.

### Router

Add `TaskType.PR_ANALYSIS` and `TaskType.FIX_COMPARISON`, both routed to
**cloud (Claude)** — they are multi-step reasoning over code. Every call is
recorded as a `RoutingDecision` like existing cloud calls.

### Agent graph

- New node `compare_to_pr`, inserted after `generate_fix` and before
  `draft_pr`.
- Runs only when agent state carries a `pr_ref` (+ `pr_diff`). The run route
  discovers the PR for the chosen issue and injects both into the initial
  state via `run_meta` / pipeline plumbing.
- Emits:
  - `benchmark.compared` — the `FixComparison` verdict, rendered as a timeline
    card with a colored badge (reuse the verify-verdict badge styling).
  - `benchmark.skipped` — quiet event when no upstream PR was found (honest,
    not hidden — same principle as the verifier's `no_repro`).
- `compare_to_pr` failures (LLM/parse/network) degrade to a `benchmark.skipped`
  with a reason, never crash the run.

### API endpoints

- `GET /api/issue-pr?repo&number` → `{pr_number, title, state, merged, url,
  changed_files} | null`. Cheap, cached.
- `POST /api/pr-analysis` `{repo, number}` → `PrAnalysis`. Discovers (cached)
  + fetches diff + one LLM call. Cached per `(repo, number)`.
- The comparison emits inside the existing `/api/run` SSE stream — **no new
  endpoint**.

### Frontend (`api/templates/app.html`, single-file)

- After issues load: fire `GET /api/issue-pr` per card. Render a "PR #N ↗"
  chip (with state) when found; nothing when not.
- Auto-trigger `POST /api/pr-analysis` for the **first 5 cards (in display
  order) that have a discovered PR**; show a spinner/loader on the card while
  pending; render the analysis inline (collapsible, like existing cards).
- Cards beyond the top 5 that have a PR get an **"Analyze PR"** button (same
  endpoint), with a small cost hint before firing (consistent with Verify).
- Timeline: render the new `benchmark.compared` event as a verdict card with a
  match/partial/divergent badge; render `benchmark.skipped` as a muted note.

## Error handling

- Discovery: Search API failure / rate-limit / no results → return `None`
  (no chip), logged. Never crashes the issue list.
- Diff fetch: failure → analysis/comparison proceeds with an empty/short diff
  and notes the gap, or degrades to skipped; never crashes.
- Analysis / comparison LLM failure → endpoint returns a structured "analysis
  unavailable" payload (analysis) or the run emits `benchmark.skipped`
  (comparison). No raised exceptions reach the user-facing stream.
- All diffs are size-capped before reaching an LLM; truncation is flagged in
  the payload so the verdict can be honest about partial input.

## Mock mode

- `use_mocks=True` or no `GITHUB_TOKEN`: discovery returns `None`, and
  analysis/comparison return deterministic stubs, so the whole UI runs offline
  and tests are hermetic.

## Testing

- `pr_discovery`: rank ordering (open > merged > closed); no-results → `None`;
  diff capping sets `truncated`; mock mode → `None`. `httpx.MockTransport`
  for HTTP, mirroring `test_comments_resolution.py`.
- `pr_analysis` / `fix_comparison`: deterministic verdicts from a stub router;
  `files_*` set arithmetic is exact; mock mode stubs.
- Agent graph: `compare_to_pr` runs and emits `benchmark.compared` when a
  `pr_ref` is present; emits `benchmark.skipped` when absent; an LLM failure
  degrades to `benchmark.skipped` rather than raising.
- API: `/api/issue-pr` and `/api/pr-analysis` shapes; mock-mode behavior;
  `/api/run` stream contains a single `benchmark.*` event.

## Sequencing (one spec, two implementation phases)

- **Phase 1** — `pr_discovery` + `pr_analysis` + `TaskType.PR_ANALYSIS` +
  `GET /api/issue-pr` + `POST /api/pr-analysis` + frontend (chips, top-5
  auto-analysis with spinners, manual "Analyze PR" button). Delivers the
  "analyze the PR" half end-to-end.
- **Phase 2** — `fix_comparison` + `TaskType.FIX_COMPARISON` +
  `compare_to_pr` node + `benchmark.compared`/`benchmark.skipped` events + run
  route PR injection + frontend verdict card. Delivers the benchmark.

## Open considerations (decided)

- Discovery runs for **all** loaded cards (cheap); LLM analysis only for the
  top 5 + manual. (Confirmed.)
- Comparison fires **automatically** as a run step when a PR exists.
  (Confirmed.)
- Primary purpose is **benchmarking the agent**; the UI leads with the
  agent-vs-PR verdict. (Confirmed.)
