# Design Spec: Buggy-file overlay (install-from-repo-source-at-buggy-commit)

**Date:** 2026-06-26
**Branch:** `feature/verify-source-overlay` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

On-demand dependency provisioning (merged 2026-06-26) fixed the
`REPRO_BROKEN`-from-missing-package wall: verify now `pip install --target`s the
issue's integration so reproducers can run. But provisioning installs the
**released wheel**, which for a closed issue usually already contains the merged
fix — so the baseline runs cleanly and verify returns `no_repro` ("baseline run
did not reproduce the failure"). Live validation confirmed this is now the
dominant verify outcome (e.g. #15962 PGVectorStore: released
`llama-index-vector-stores-postgres-0.8.1` already carries PR #21447's fix →
`no_repro`).

`no_repro` is honest but uninformative: with no baseline reproduction we can't
tell whether the agent's fix actually works. We need the integration in its
**pre-fix (buggy) state** so the baseline reproduces and the rerun yields a real
verdict (VERIFIED / STILL_BROKEN / MASKS_SYMPTOM).

## Goal & success criterion

Reconstruct the pre-fix state of the bug's files inside the verify sandbox so the
baseline reproduces, then evaluate the agent's fix against it.

**Success:** re-run #15962 (or any closed issue whose fix is in the released
wheel) → after the released-wheel baseline returns `no_repro`, verify overlays
the buggy version of the PR-changed files, the baseline-retry reproduces, the
agent's patch is applied, and `verify.result` becomes a real verdict instead of
`no_repro`.

## Decisions (locked in brainstorming)

1. **Approach: buggy-file overlay at SHA.** Provision the released wheel (existing
   path), derive the buggy commit from the fixing PR, fetch ONLY the PR-changed
   files at that commit, and overlay them onto the installed module. Chosen over
   reverse-applying the PR diff (unified-diff fuzz, truncation) and over a full
   source/tarball install (needs git in the image + a full monorepo clone).
2. **Buggy SHA = the fixing PR's `merge_commit_sha` → its first parent** (mainline
   immediately before the fix merged: bug present, fix absent). Fallback: the PR
   `base.sha`. `None` on any failure → no overlay.
3. **Trigger: lazy — only after a `no_repro` baseline.** Provision + baseline
   first; overlay (and its API calls) happen only when the released wheel masks
   the bug. Naturally handles both cases (unreleased fix → baseline already
   reproduces, no overlay; released fix → overlay).
4. **Reuse the patch-apply machinery.** The overlay is `apply_changes` with the
   pre-fix content before the baseline-retry; the agent's fix is a second
   `apply_changes` before the rerun (it overwrites the same module origin via the
   bootstrap's `find_spec`→copyfile). No git, no clone, no new sandbox method.

## Control flow

```
provision released wheel  →  baseline()
  ├─ baseline reproduces (fix not yet released)  → apply agent patch → rerun   [unchanged]
  └─ no_repro AND source_overlay AND pr_number known AND pr_files non-empty:
        buggy_sha = code_host.buggy_parent_sha(pr_number)   # merge_commit parent; fallback base.sha
        overlay_changes = []
        for f in pr_files:                                  # the human PR's changed files
            if installed_module_path(f) is None:  continue   # skip docs/tests/notebooks
            content = code_host.get_file_at_ref(f, buggy_sha)
            if content is not None:
                overlay_changes.append(FileChange(path=f, patched_content=content, rationale="buggy overlay"))
        if overlay_changes:
            emit verify.overlay {sha, files, ok}
            handle.apply_changes(overlay_changes)   →  baseline-retry()
              ├─ reproduces → apply agent patch (handle.apply_changes(fix.changes)) → rerun → real verdict
              └─ still no_repro → honest no_repro (degraded, as today)
```

The overlay and the fix may touch the same file (the bug file IS the fix file);
the baseline-retry writes the buggy version, the rerun writes the agent's fix —
each `apply_changes` re-stages the manifest and the next `run()` applies it.

## Components

| File | Change |
|------|--------|
| `integrations/github.py` | Two methods on the `_CodeHostLike` Protocol + `GitHubClient` + `MockGitHubClient` + `DryRunCodeHost` (the code host already owns the repo + token and does PR ops, so the verifier needs no token/repo threading): (1) `get_file_at_ref(self, path, ref) -> str \| None` — GitHubClient via `repo.get_contents(path, ref=ref)` decoded; Mock canned; `None` on miss/error. (2) `buggy_parent_sha(self, pr_number) -> str \| None` — GitHubClient via `repo.get_pull(n).merge_commit_sha` → `repo.get_commit(sha).parents[0].sha`; fallback `repo.get_pull(n).base.sha`; Mock canned; `None` on any error. Both follow the existing code-host convention (real PyGithub path live-validated, Mock unit-tested). |
| `verification/verifier.py` | `source_overlay: bool = True` ctor kwarg (mirrors `provision_deps`); `pr_number: int \| None` and `pr_files: list[str] \| None` args on `verify()` (per-run data, like `issue_body`); the lazy `no_repro` → overlay → baseline-retry branch (extract a `_reproduced(result, repro)` helper, reused for baseline + retry); build overlay `FileChange`s from `pr_files` (filtered by `installed_module_path`) via `code_host.buggy_parent_sha` + `code_host.get_file_at_ref`; emit `verify.overlay`; gate on `self.source_overlay`; all wrapped so any failure degrades to the existing `no_repro`. |
| `api/routes/verify.py` | reconstruct `pr_number` (the `benchmark.compared` `pr_number` field) and `pr_files` (`files_both` + `files_theirs_only`) from the persisted run; pass `source_overlay=settings.verify_source_overlay` to the `Verifier` ctor and `pr_number=`/`pr_files=` to `verify()`. |
| `config.py` | `verify_source_overlay: bool = True` (env `TVASTR_VERIFY_SOURCE_OVERLAY`). |
| `tests/conftest.py` | seal `TVASTR_VERIFY_SOURCE_OVERLAY=false` (no network in tests). |
| `api/templates/app.html` | add `verify.overlay` to the verify event allowlist + a summary label (mirrors `verify.provision`). |

## Confidence / gate / PII

Unchanged. The overlay touches only the verify sandbox; it sends nothing new to
any cloud LLM. The only network egress is GitHub reads (PR metadata + file
contents at a ref) — the same surface `discover_pr`/`fetch_pr_diff` already use,
behind the existing GitHub token.

## Error handling (never crashes the run)

- No fixing PR known, `verify_source_overlay=false`, `buggy_parent_sha` returns
  `None`, no PR-changed file maps to a module, or every `get_file_at_ref` returns
  `None` → no overlay; verify returns the existing honest `no_repro`.
- Overlay applied but baseline-retry still doesn't reproduce → honest `no_repro`.
- Any exception in the overlay path is caught and logged; verify proceeds to the
  `no_repro` finish. No new `Verdict` value, no new crash surface.

## Observability

New event `verify.overlay` (payload `{sha, files, ok}`) emitted once, just before
the overlay `apply_changes`. The existing `verify.baseline` fires again for the
retry (so the timeline shows baseline → overlay → baseline). `verify.result`
carries the final real verdict.

## Out of scope (explicit next steps)

- **Whole-package source install** for bugs that span files the PR didn't change,
  or that span integration + core. The overlay only reconstructs the PR-changed
  files; a multi-file/core-spanning bug may still `no_repro`. Recorded follow-up
  (the heavier git/tarball install).
- **Reproducers that need a live service** (e.g. embedded Weaviate downloading a
  server binary, blocked by `--network=none`/`--read-only`). Separate frontier.
- **Investigator non-determinism** (a run returning confidence 0.0 on an issue it
  solved before). Unrelated to verify.

## Testing (offline)

- `buggy_parent_sha` / `get_file_at_ref`: unit-tested on `MockGitHubClient`
  (canned), matching how the other code-host methods are tested; the real
  PyGithub path is exercised live (as with `get_file`/`open_pull_request`).
- Verifier lazy overlay (fake host + fake sandbox, scripted `RunResult`s):
  - first baseline `no_repro` → overlay fetched + `apply_changes(buggy)` →
    baseline-retry reproduces → `apply_changes(fix)` → rerun clean → VERIFIED;
    assert `verify.overlay` emitted and `apply_changes` called with buggy then fix.
  - first baseline reproduces → NO overlay (assert `verify.overlay` not emitted).
  - `no_repro` but no `pr_number` (or flag off) → NO overlay, stays `no_repro`.
  - overlay applied but baseline-retry still `no_repro` → honest `no_repro`.
- Live metric: #15962 → after released-wheel `no_repro`, overlay reproduces and
  `verify.result` is a real verdict (not `no_repro`).
