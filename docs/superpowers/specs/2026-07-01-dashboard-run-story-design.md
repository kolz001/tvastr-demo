# Design Spec: Dashboard run story card + chapter dividers

**Date:** 2026-07-01
**Branch:** `feature/dashboard-run-story` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

The pipeline panel in `src/tvastr/api/templates/app.html` is a pretty-printed
event log: every event gets equal visual weight, labels are internal jargon
(`verify.repro_synth`, `baseline exit=1 · original_exception_seen=true`), and
there are 13 verdict strings. Transparency is total; comprehension requires
already knowing the pipeline. The primary audience is a **first-time viewer
watching a ~5-minute demo** (live or replayed): today nothing
answers "what did the agent conclude, did the fix verify, did it match the
human PR?" without scrolling the wall and decoding badges.

## Goal & success criterion

An unfamiliar viewer can answer, at a glance and at any moment during a live
or replayed run: *what issue is this, what did the agent diagnose, what did
it change, is the fix proven, and does it agree with the human fix?* —
without losing one bit of the existing raw transparency.

**Success:** replaying persisted runs (including #17105's verified run) shows
a correct, fully-populated story card whose every row click-expands its
evidence event; a live mock run assembles the card in real time; the raw
timeline is unchanged beneath it.

## Decisions (locked in brainstorming)

1. **Deterministic "run story" card** over a chapter-only restructure or an
   LLM-written narrative. The card is derived 1:1 from events — no LLM call,
   no latency, and no risk of embellishment in exactly the artifact whose
   selling point is honest verdicts. A static gloss map says only what the
   events prove.
2. **Plus light chapter dividers** in the timeline (user-requested addition):
   plain-English section headers, no nesting/collapsing — the card handles
   at-a-glance, dividers just orient scrolling.
3. **Covers both live and replay.** `replayRun` already funnels through
   `appendEvent`, so one hook serves both.

## Design

### 1. Run story card

A `#story-card` panel pinned at the top of the pipeline pane. Appears when
`pipeline.start` arrives; rows fill in as their source events land. Unfilled
rows show a dimmed "…" so the story visibly assembles during a live run.

| Slot | Source event(s) | Rendering |
|------|-----------------|-----------|
| **Issue** | `pipeline.start` | `repo #issue_number — issue_title` |
| **Diagnosis** | `agent.node.end` (step=investigate: `summary`, `confidence`); `agent.node.start` (step=confidence_gate: `confidence`, `threshold`, `decision`) | summary sentence + chip `confidence 0.90 ≥ 0.70 → acting` (or `→ skipped`) |
| **Fix** | `fix.generated` (`files`, `register`, `summary`) | `N file(s) · <register> — <summary>` |
| **Verification** | `verify.result` (`verdict`, `oracle`, `elapsed_s`) | verdict badge + `VERDICT_GLOSS` sentence; pending state: "not yet verified"; attempt count when >1: `attempt 2 · verified · reproducer` |
| **vs. human fix** | `benchmark.compared` (`verdict`, `same_root_cause`, `pr_number`, `files_both`, `files_theirs_only`) or `benchmark.skipped` (`reason`) | `Same root cause as PR #21543 · files overlap 2/3` / skipped reason |

An `error` event marks the row for its stage red with the error message.

**Evidence links (the transparency mechanism):** each filled row records the
DOM node of its source event card; clicking the row scrolls to that card and
expands it. Every claim in the card is one click from its raw evidence. The
timeline below is untouched.

### 2. `VERDICT_GLOSS`

A static map: one honest plain-English sentence per verdict (all 13 in
`VERDICT_BADGE`), e.g.:

- `verified_via_reproducer` → "Reproduced the crash on the buggy code,
  applied the agent's fix, re-ran — crash gone."
- `no_repro` → "Couldn't reproduce the bug in the sandbox, so the fix is
  unproven — an honest 'no evidence', not a failure."
- `repro_broken` → "The test script itself broke, so it can't be trusted as
  evidence either way."

Used in three places: the story card's Verification row, the
`verify.result` event card body, and as a `title` tooltip on verdict badges
in the Past-runs table.

### 3. Chapter dividers

Thin plain-English dividers inserted into the timeline when the **first**
event of each chapter arrives (deterministic event-type-prefix → chapter
mapping; a seen-set guarantees once each):

| Chapter title | Triggering prefixes |
|---------------|---------------------|
| Reading the failure | `ingest.`, `detect.`, `threshold.` |
| Investigating the cause | `agent.`, `tool.`, `retrieval.`, `doc.`, `router.`, `llm.` |
| Writing the fix | `fix.`, `pr.` |
| Comparing to the human fix | `benchmark.` |
| Proving it in a sandbox | `verify.` |

Events matching no prefix (e.g. `notify.`, `audit.`, `pipeline.`, `error`)
insert no divider. Because `llm.`/`router.`/`tool.` events also occur in
later chapters, the mapping is **first-match-wins by chapter order**: a
prefix only triggers its chapter if no later chapter has started — simplest
correct rule: once a chapter's divider is inserted, earlier chapters are
sealed and their prefixes are ignored. The existing "Verification attempt N"
divider stays, restyled to match as a sub-divider under the verify chapter.

### 4. Lifecycle

- Story card + seen-chapters set reset wherever `#pipeline` is cleared today:
  starting a new run stream and `replayRun`.
- Multiple verify attempts update the Verification row **in place** — it
  always shows the latest verdict, with the attempt count.

### 5. Degradation (never crashes, never lies)

- Older persisted runs missing newer payload fields (e.g. `register`) render
  "—" for that fragment; a missing event leaves the row in its pending state.
- Unknown verdict → yellow badge with the raw verdict string, gloss omitted.
- All rendering goes through the existing `html()` escaper.

## Components

| File | Change |
|------|--------|
| `src/tvastr/api/templates/app.html` | `VERDICT_GLOSS`, `CHAPTERS`, story-card markup + CSS, `updateStory(event)` and `maybeInsertDivider(event)` called from `appendEvent`, resets in run-start/`replayRun`, gloss tooltips in the runs table, restyled attempt divider. (~200 lines) |

No backend, event-schema, or Python changes. The existing pytest suite is
untouched and must stay green.

## Testing / verification plan

Backend tests: none needed (no Python change); run the suite once to confirm
green. UI verification (manual, via the running app):

1. Replay 2–3 real persisted runs — #17105's verified run and an older
   pre-register run — confirm all five rows fill correctly (or degrade to
   "—"), evidence-clicks expand the right cards, dividers appear exactly
   once each in order.
2. Run a fresh mock-mode issue live — confirm the card assembles in real
   time and the pending "…" states render.
3. Trigger a verify re-run — confirm the Verification row updates in place
   with the attempt count.
4. Past-runs table — hover a verdict badge, confirm the gloss tooltip.

## Scope boundary (deferred)

- Collapsible/nested chapters — YAGNI; the card carries at-a-glance.
- LLM-written run narratives — rejected for transparency risk.
- Story card in the Past-runs *table* itself (beyond tooltips) — the table
  already links to replay, which now shows the card.
