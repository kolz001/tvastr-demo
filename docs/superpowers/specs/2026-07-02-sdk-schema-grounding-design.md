# Design Spec: SDK-schema grounding

**Date:** 2026-07-02
**Branch:** `feature/sdk-schema-grounding` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

On llama_index #19293 the pipeline now completes (forced final synthesis), but
the diagnosis is wrong in a way no available evidence channel could correct:
the real bug is that Gemini 2.5 models emit output tokens under a **renamed
field** (`response_token_count` instead of `candidates_token_count`). The
agent diagnosed "token counts never mapped into `additional_kwargs`" — true at
the issue-era SHA, but not the reported symptom — and `benchmark.compared`
returned `divergent` vs PR #21897 (confidence 0.88).

The rename clue was unreachable everywhere the agent looks today:

- the issue body's key evidence is a **screenshot image**;
- the issue's two comments don't mention it;
- doc-grounding web-searched, but with queries built from the story it
  already believed (normalization gap) — its 2 sources confirmed the wrong
  mechanism;
- the one authoritative, locally-obtainable source — the **`google-genai`
  SDK's own type definitions** — was never consulted, even though the
  grounding prompt explicitly names "a renamed field … in a dependency" as
  its trigger condition, and the verify sandbox already knows how to install
  exactly these packages.

## Goal & success criterion

When the diagnosis hinges on a third-party response shape, give the grounding
step the installed SDK's relevant type definitions as ground truth for field
names.

**Success (live):** re-run #19293 — the probe names `google-genai`, extraction
surfaces the usage-metadata class (which lists `response_token_count` next to
`candidates_token_count`), grounding corrects the diagnosis to the field
rename, the fix handles it, and `benchmark.compared` moves from `divergent`
to `match`/`partial` with `same_root_cause=true` vs PR #21897.

## Decisions (locked in brainstorming)

1. **Hook: the grounding step** (`_ground_root_cause`) — not a new
   investigator tool action (costs scarce rounds; bigger plumbing) and not
   both. The step's whole job is already "validate against external reality";
   #19293's wrong story got *confirmed* exactly there.
2. **Acquisition: host pip, wheels-only** —
   `pip install --only-binary=:all: --no-deps --target data/sdk_cache/…`.
   No setup.py execution (wheels only), no Docker on the diagnosis path,
   seconds when cached. Same trust level as the host's existing GitHub/PyPI
   traffic. (Docker-provisioning reuse and download+unpack were rejected as
   heavier for the same result.)
3. **Selection/extraction: LLM names, code greps** — one cheap structured
   call names the package + 2-4 schema keywords; deterministic code extracts
   matching class definitions. Model supplies judgment, code supplies truth.
   (Fully-deterministic selection is brittle across multi-dep integrations; a
   grounding-side tool loop is a bigger change than this slice needs.)

## Design

### Flow (inside `_ground_root_cause`, before the existing grounding call)

1. **Schema probe** — one structured LLM call (new `TaskType.SCHEMA_PROBE`,
   routed/redacted like every other task): input is the root-cause summary,
   suspected files, and an issue snippet; output is strict JSON
   `{"relevant": bool, "package": str, "version_hint": str|null,
   "keywords": [str, ...]}`. `relevant: false` → skip everything.
2. **Fetch** — `fetch_sdk(package, version_hint) -> Path | None`:
   `pip install --only-binary=:all: --no-deps --target
   data/sdk_cache/<package>-<version|latest>/` via list-form subprocess (no
   shell). Cache hit skips pip entirely. A failing version pin is retried
   once without the pin (latest). Version-pinning to the issue-era dep
   version stays a recorded follow-up.
3. **Extract** — `extract_schema_snippets(root, keywords, max_snippets=6,
   max_lines_each=40) -> list[Snippet]`: walk the package's `.py`/`.pyi`
   files, pull `class …:` blocks whose body contains any keyword
   (case-sensitive substring), cap snippet count and lines per snippet.
4. **Inject** — prepend a labeled block to the grounding prompt:
   *"Authoritative type definitions from the installed `<package>` SDK
   (ground truth for field names):"* followed by the snippets (each tagged
   with its file path). Web search remains available; the grounding response
   contract is unchanged.
5. **Observe** — new event `doc.sdk_schema`
   `{package, version, ok, reason?, snippets, files}` emitted in every path
   (skip reasons included); dashboard `summarize` case renders it.

### Safety / trust

- Package name is LLM-supplied → validated against
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` (length-capped) before any subprocess.
- Wheels-only + `--no-deps` + isolated `--target`: nothing executes at
  install time, no transitive installs, nothing on `sys.path`.
- Extracted snippets are read as text into a prompt; the cache is never
  imported or executed. `data/sdk_cache/` is gitignored.

### Degradation ladder (never aborts, never worse than today)

Each rung logs + emits `doc.sdk_schema` with `ok:false` and a `reason`, then
grounding proceeds un-enriched: probe `relevant:false` → skip; probe JSON
unparseable → skip; invalid package name → skip; pip failure (offline / no
wheel / bad pin after the un-pinned retry) → skip; zero keyword matches →
skip. Flag off → no probe call at all (today's behavior exactly).

## Components

| File | Change |
|------|--------|
| `src/tvastr/agent/sdk_schema.py` (new) | probe prompt + `SchemaProbe` parse/validation, `fetch_sdk`, `extract_schema_snippets`, `Snippet` |
| `src/tvastr/agent/graph.py` | wire probe→fetch→extract→inject into `_ground_root_cause`; emit `doc.sdk_schema` |
| `src/tvastr/llm/router.py` | `TaskType.SCHEMA_PROBE` + routing entry |
| `src/tvastr/config.py` | `sdk_schema_grounding: bool = True` (`TVASTR_SDK_SCHEMA_GROUNDING`) |
| `tests/conftest.py` | seal `TVASTR_SDK_SCHEMA_GROUNDING=false` |
| `src/tvastr/api/templates/app.html` | `summarize` case for `doc.sdk_schema` |
| `.gitignore` | `data/sdk_cache/` |

## Testing (offline; no network in tests)

- `extract_schema_snippets` against a fabricated package tree (tmp_path):
  finds classes containing keywords, respects caps, handles `.pyi`, `[]` on
  no match.
- Probe parsing: valid JSON → probe object; garbage / `relevant:false` /
  malicious package names (`"; rm -rf"`, `../../etc`) → rejected.
- `fetch_sdk` with mocked subprocess: asserts wheels-only/no-deps/target
  argv, pin-retry-without-pin, cache-hit skips pip.
- `_ground_root_cause` wiring with scripted router + monkeypatched fetch:
  enriched prompt contains the labeled snippet block; every degradation rung
  leaves the prompt identical to today's; `doc.sdk_schema` payloads asserted.
- Flag sealed off → zero probe calls; existing suite unchanged.

## Scope boundary (deferred, recorded)

- Version-pinning to the issue-era dependency version (resolve the
  integration's pin → SDK version).
- Investigator-side `inspect_dependency` action.
- Feeding schema evidence directly to `fix_generation` (it inherits the
  corrected root cause, which is the load-bearing path).
