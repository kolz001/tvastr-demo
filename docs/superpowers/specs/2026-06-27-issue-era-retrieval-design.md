# Design Spec: Issue-era per-file retrieval

**Date:** 2026-06-27
**Branch:** `feature/issue-era-retrieval` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

On llama_index issue [#17105](https://github.com/run-llama/llama_index/issues/17105)
(`'GenerateResponse' object has no attribute 'items'`) the investigator reasoned
correctly to the right subject in round 1, then **gave up at confidence 0.0**
because it could not find the source:

- `search_code("GenerateResponse")` → 0 hits; `search_code("OllamaMultiModal")`
  → 1 irrelevant `mappings.json`. GitHub code-search only indexes the default
  branch and is weak on identifiers.
- `list_dir(".../llama-index-multi-modal-llms-ollama")` → **404**. The entire
  `multi_modal_llms` integration was *removed* from `main` (consolidated into
  `llama-index-llms-ollama`), so the buggy file does not exist at HEAD.

Root cause: **the agent reads current `main`, but issues are historical and the
codebase has drifted.** This is the session's deepest recurring theme — the verify
overlay fixed it for *reproduction*; investigation/retrieval still reads `main`.

A Claude-driven session solved #17105 by: reading the issue (exact error +
`base.py:28` + `ollama 0.4.2`), recognizing the file was gone from `main`,
recovering the buggy file from history (commits API on the path, anchored to the
issue date), grepping that file for the access-pattern asymmetry, then confirming
empirically. The retrieval lesson: **recover the file as it was when the bug was
reported.**

## Goal & success criterion

Let the investigator read repository code **as of the issue's creation date**, so
moved/renamed/deleted paths resolve and the code still contains the bug.

**Success:** re-run #17105 — at the issue-era ref, `list_dir` of the 2024
`multi_modal_llms` tree returns entries (not 404), `get_file` of the ollama
`base.py` returns the buggy source, the investigator roots the cause (the
`.items()`-on-a-pydantic-response bug) and produces a confident `RootCause`
(gate → act) instead of confidence-0.0 escalation.

## Decisions (locked in brainstorming)

1. **Anchor on the issue, not the fix.** Resolve the issue-era commit from the
   issue's **creation date** (already captured as `sample_events[0].timestamp`),
   never from the fixing PR. Works on open issues; uses no fix information.
2. **Per-file recovery, not a whole-repo tarball.** Fetch individual files at the
   issue-era ref via the GitHub API (reusing `get_file_at_ref`) — light, surgical,
   handles deleted-on-`main` files. (The whole-repo tarball + ripgrep search
   substrate is the deferred follow-up for the "issue names no file" case.)
3. **Single issue-era ref = repo HEAD as-of the issue date** (one commits-API
   call). A file's content at that tree equals its last edit before the issue —
   i.e. the issue-era version — so a single ref is both correct and simple.
4. **Transparent to the investigator:** wrap the code host so `get_file`/`list_dir`
   serve the issue-era version; the investigator's tools are unchanged.

## Architecture

```
issue_date = sample_events[0].timestamp            # already captured
issue_era_sha = code_host.commit_before(issue_date)  # commits?until=date&per_page=1
if issue_era_retrieval and issue_era_sha:
    ctx.code_host = IssueEraCodeHost(inner=ctx.code_host, sha=issue_era_sha)
    emit retrieval.issue_era {sha, ok}
# the investigator (graph._investigate) seeds suspected files from BOTH
# extract_stack_files(sample_events) AND extract_issue_files(issue_body), and
# now reads/list_dirs at the issue era; search_code/PRs unchanged
```

`IssueEraCodeHost.get_file(path)` → `inner.get_file_at_ref(path, sha)` (fallback
`inner.get_file`); `list_dir(path)` → `inner.list_dir_at_ref(path, sha)`;
`search_code`, `open_pull_request`, `buggy_parent_sha`, `get_file_at_ref`,
`commit_before` → delegate to `inner`.

## Components

| File | Change |
|------|--------|
| `integrations/github.py` | `commit_before(self, iso_date: str) -> str \| None` (commits API `?until=<date>&per_page=1`, first sha; `None`/error → `None`) and `list_dir_at_ref(self, path: str, ref: str) -> list[str]` (contents API with `ref`), added to the `CodeHost` + `_CodeHostLike` Protocols + `GitHubClient` + `MockGitHubClient` (canned) + `DryRunCodeHost` (delegate). Reuses the existing `get_file_at_ref`. |
| `integrations/issue_era_host.py` (new) | `IssueEraCodeHost(inner, sha)` implementing the code-host protocol per the architecture above. |
| `agent/retrieval/issue_extract.py` (new) | `extract_issue_files(issue_body: str) -> list[str]` — parse traceback `File "…", line N` paths; best-effort map install/import path → repo path: `llama_index/<cat>/<name>/<rest>` → `llama-index-integrations/<cat>/llama-index-<cat with _→->-<name>/llama_index/<cat>/<name>/<rest>`; `llama_index/core/<rest>` → `llama-index-core/llama_index/core/<rest>`. Unmappable paths are dropped (best-effort seed; issue-era `list_dir` is the safety net). Deduped. |
| `pipeline.py` (modify) | resolve `issue_era_sha` via `code_host.commit_before(issue_date)`; gate on `settings.issue_era_retrieval`; wrap `ctx.code_host` with `IssueEraCodeHost`; emit `retrieval.issue_era`. |
| `agent/graph.py` (modify) | in `_investigate`, merge `extract_issue_files(state["issue_body"])` into the existing `extract_stack_files(sample_events)` seed (deduped). |
| `config.py` | `issue_era_retrieval: bool = True` (`TVASTR_ISSUE_ERA_RETRIEVAL`). |
| `tests/conftest.py` | seal `TVASTR_ISSUE_ERA_RETRIEVAL=false` (tests use the mock host; no network). |
| `api/templates/app.html` | render `retrieval.issue_era` in the timeline (allowlist + summary), mirroring existing event rows (Minor). |

## How the seed reaches the investigator

The investigator (`graph._investigate`) already seeds suspected files from
`extract_stack_files(sample_events)`. `extract_issue_files(issue_body)` is merged
into that same seed (deduped), so a traceback-named file (e.g. `base.py:28`)
becomes a starting `read_file` target. The investigator otherwise navigates with
its existing `list_dir`/`search`/`read` actions — now reading issue-era content.
The `retrieval.issue_era` event payload is therefore `{sha, ok}` (no
`seeded_files`, which the investigator emits per-round as `tool.call`s).

## Scope boundary (deferred, recorded follow-ups)

- **Repo-wide issue-era search.** `search_code` stays current-`main` (GitHub
  code-search cannot query a ref). The "issue names no file → need search" case is
  the tarball-snapshot + ripgrep follow-up. With good traceback extraction +
  issue-era `list_dir` navigation, it is needed less often.
- **Dynamic confirmation.** Building a venv at the reported dependency version and
  reproducing through the real entry point (the Claude session's steps 5–6) is the
  recorded investigator-sandbox frontier — the next feature after this.

## Error handling (never crashes)

- No issue date / `commit_before` returns `None` / wrap fails → no wrap; today's
  current-`main` reads (no regression).
- `get_file_at_ref`/`list_dir_at_ref` miss at the ref → `IssueEraCodeHost.get_file`
  falls back to `inner.get_file`; `list_dir` returns `[]` (as today).
- `extract_issue_files` parse/map failure → no seed.
- All failures logged; degrade. No new crash surface. `flag=false` → exact
  current behavior.

## Observability

`retrieval.issue_era` event (payload `{sha, ok}`) emitted once at wrap time;
rendered in the dashboard timeline.

## Testing (offline)

- `IssueEraCodeHost` over a mock inner: `get_file`/`list_dir` call the inner's
  `_at_ref` methods with `sha`; `search_code`/`open_pull_request`/`buggy_parent_sha`
  delegate unchanged.
- `commit_before` + `list_dir_at_ref` on `MockGitHubClient` (canned, deterministic,
  offline).
- `extract_issue_files`: a sample traceback → mapped repo paths; cases for core,
  an integration (`multi_modal_llms/ollama`), and an unmappable path (dropped).
- Degradation: no `sha` → inner host returned unchanged; `flag=false` → no wrap.
- Live metric: #17105 → issue-era `list_dir`/`get_file` resolve the 2024 ollama
  `base.py`; investigator produces a confident root cause (gate → act).
