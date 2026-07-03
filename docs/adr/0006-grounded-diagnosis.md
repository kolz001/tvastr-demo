# 6. Ground diagnosis in issue-era code and installed-SDK reality

- Status: Accepted
- Date: 2026-07-02

## Context

The original diagnosis core (ADR-covered implicitly in the weeks 1-2 design:
`reason_root_cause` → bounded `expand_context`) was a single cloud call plus
at most a couple of follow-up rounds, always reading the code host's default
branch. Three failure modes showed up once the agent was pointed at real,
closed LlamaIndex issues instead of curated fixtures:

1. **The investigator ran out of budget one step from the answer.** On
   llama_index #19293, four rounds of one-file-at-a-time reads left the model
   with almost everything it needed, but the round budget expired before it
   emitted a `root_cause`. The pipeline discarded every file it had read and
   fell back to a hardcoded 0.0-confidence escalation — a worse outcome than
   just asking the model to answer with what it had.
2. **The agent read the wrong point in time.** Issues are historical; `main`
   drifts. On llama_index #17105, the entire integration the bug lived in had
   been removed/consolidated by the time the agent looked, so `list_dir` and
   `search_code` against `main` returned nothing — not because there was no
   bug, but because the agent was looking at the wrong commit.
3. **Web-search grounding confirmed a story instead of testing it.** Also on
   #19293: resolving the diagnosis hinged on the third-party SDK's
   (`google-genai`) actual usage-metadata shape for the models in question —
   knowledge that was unreachable everywhere the agent looked. The issue's
   key detail was a screenshot, the comments didn't mention it, and nothing
   in the pipeline ever read the installed SDK's own type definitions.
   Worse, doc-grounding's web search ran queries *built from the story it
   already believed*, and its sources duly confirmed that story.
   Confirmation bias, not absence of a search step, was the failure.
   (Postscript, same issue: the linked human fix keyed on a field that later
   analysis showed doesn't exist on the affected code path at all — the
   benchmark's reference PR can itself be wrong, which is why grounding must
   check primary sources, not just agree with whichever fix exists.)

## Decision

Replace the single-shot diagnosis core with an **agentic investigator**, and
add a **grounding step** between investigation and fix generation that
consults two independent, deterministic sources of truth rather than relying
solely on the model's own search judgment.

**`investigate`** — a bounded agentic tool loop (JSON-vocabulary
propose→execute, not native tool-use) over three actions: `search`
(`search_codebase`), `read_file` (`retrieve_code_files`), `list_dir`. Capped
at `_MAX_INVESTIGATE_ROUNDS = 4`. Round 1 is **free-seeded**: files named in
the stack trace and in the issue body's own traceback text are fetched
eagerly before the model spends a round asking for them. On exhausting the
round budget without a `root_cause`, a `for`/`else` (so existing
early-`break` convergence paths are untouched) fires exactly once and forces
**one additional tool-less synthesis call** over everything accumulated —
including files fetched in the very last round that the model's own JSON
response never got to see — asking it to answer now, with an honestly low
confidence if it genuinely can't converge. Only if that call also fails to
produce a `root_cause` does the pipeline fall back to the original
0.0-confidence escalation. The fix is strictly additive: every path that
worked before still works identically.

**Issue-era code reads** — all `investigate` reads go through an
`IssueEraCodeHost` wrapping the real/mock code host, resolving `get_file`/
`list_dir` at the commit that existed when the issue was filed
(`commit_before(issue_date)`), falling back to `main` if the era-pinned read
comes back empty. `search_code` still queries `main` (GitHub's code-search
API can't be scoped to an arbitrary ref) — this is a known, recorded gap, not
an oversight. Anchoring to issue-filing time rather than fix-merge time means
this works on issues that were never fixed, and surfaces files as they
existed at report time rather than treating "moved or deleted at HEAD" as
evidence of "no bug here."

**`ground_root_cause`** gained a second, independent evidence channel
alongside the existing web-search grounding (itself hardened earlier to
force `tool_choice` on the `web_search` tool rather than leave it to the
model's discretion, after search was observed being skipped nondeterministic
across otherwise-identical runs). **SDK-schema grounding**: a cheap
structured call (`SCHEMA_PROBE`) asks whether the bug plausibly hinges on a
third-party SDK's response shape and, if so, names the package and a few
keywords. If relevant, tvastr does a **wheels-only, `--no-deps`,
isolated-`--target` `pip install`** of that package on the host (never
imported or executed — `data/sdk_cache/` is read as text only), extracts the
class definitions whose bodies contain a probe keyword (round-robin budget
allocation across keywords so a common one can't starve a rare one), and
prepends them to the grounding prompt as "ground truth for field names,"
with an explicit instruction to cross-check every field the suspect code
reads against them. The package name is validated against a strict pattern
before any subprocess call runs.

## Why these choices

- **Forced final synthesis, not a larger round budget.** Raising
  `_MAX_INVESTIGATE_ROUNDS` trades cost for the same failure mode at a higher
  threshold. The actual defect was throwing away evidence at the boundary;
  fixing that is strictly cheaper (one extra call, only on the rare
  exhaustion path) than paying for more rounds on every run.
- **Issue-era reads over "just read `main`."** A code host that always
  reads the tip of the default branch is silently making a claim — "the code
  I'm showing you is what existed when this bug was reported" — that becomes
  false the moment a file is renamed or removed. Pinning to the issue's own
  timestamp keeps that claim honest without requiring per-issue manual setup.
- **SDK schema extraction over a bigger/smarter web search.** Web search
  grounds a claim against whatever pages a query returns, and a query is
  itself a product of the model's current belief — a self-reinforcing loop
  when that belief is wrong. An installed SDK's own type definitions are not
  a search result; they're the actual contract the code is calling into, and
  extracting them deterministically (LLM names the target, code greps it) is
  cheaper and more reliable than asking the model to search its way to
  the same file.
- **Wheels-only, no-deps, isolated target.** The fetch step handles
  LLM-supplied package names, so it is treated as untrusted input: no
  `setup.py` execution, no transitive installs, nothing added to `sys.path`,
  strict name validation before any subprocess call.

## Consequences

- **Diagnosis is no longer purely a function of what the model chooses to
  search for.** Two of its evidence channels (issue-era reads, SDK schema
  extraction) are deterministic and independent of the model's own
  confidence in its story, which is precisely what let #19293 recover from a
  self-confirming wrong diagnosis.
- **Static schema evidence has a real ceiling.** SDK-schema grounding proves
  that a field named `response_token_count` *exists* on a class definition;
  it says nothing about which field is actually *populated* at runtime for
  the specific response the issue reporter received. It narrows the search
  space for a diagnosis — it is not a substitute for executing the code path
  in question. This is the honest boundary of the feature, not a rounding
  error to fix later.
- **Evidence interpretation, not evidence gathering, is the residual
  bottleneck.** The investigator can now retrieve era-correct source and
  SDK-authoritative type definitions in the same run; whether the model
  correctly reads and reconciles both against its working theory is a
  judgment problem the retrieval layer cannot fully solve for it.
- **New operational surface.** `data/sdk_cache/` grows with every distinct
  `(package, version)` grounded against; it's gitignored and treated as a
  local cache, not a versioned artifact.
- **Cost.** One additional structured call (`SCHEMA_PROBE`) per grounding
  attempt, plus an occasional `pip install` (cached across runs for the same
  package/version). Both are skipped entirely when the probe says the bug
  isn't SDK-schema-shaped, or when `sdk_schema_grounding` is disabled.
