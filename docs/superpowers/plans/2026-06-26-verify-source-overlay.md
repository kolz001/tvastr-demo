# Verify Source-Overlay (buggy-file overlay) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the released wheel provisioned by verify already contains the merged fix (today's `no_repro`), overlay the *pre-fix* version of the fixing-PR's changed files so the baseline reproduces, turning `no_repro` into a real verdict.

**Architecture:** Lazy, in the verifier. After a `no_repro` baseline, derive the buggy commit from the fixing PR (`merge_commit_sha`→first parent, via the code host), fetch the PR-changed files at that commit (code host `get_file_at_ref`), and overlay them with the existing `apply_changes`/bootstrap machinery, then re-baseline. No git, no clone. The PR number + file list are reconstructed from the persisted `benchmark.compared` event.

**Tech Stack:** Python 3.11/3.12, PyGithub (code host), pydantic-settings, Docker sandbox, pytest.

## Global Constraints

- **Never crash the verify run.** Every step of the overlay path is wrapped so any failure (no PR, SHA derivation fails, fetch fails, overlay still doesn't reproduce) degrades to the existing honest `no_repro`. No new `Verdict`.
- **Lazy trigger.** The overlay runs ONLY after a `no_repro` baseline, AND `self.source_overlay` is true, AND `pr_number` is known, AND `pr_files` is non-empty.
- **Buggy SHA = fixing PR's `merge_commit_sha` → first parent**, fallback the PR `base.sha`, else `None`.
- **Scope filter:** overlay only `pr_files` entries that map to an importable module (`installed_module_path(path) is not None`) — skip docs/tests/notebooks.
- **Reuse `apply_changes`** for the overlay (buggy content) and again for the agent fix; no new sandbox method.
- **`pr_files` source:** the persisted `benchmark.compared` payload's `files_both` + `files_theirs_only` (the human PR's changed files). No new network fetch.
- **Config flag** `verify_source_overlay: bool = True` (env `TVASTR_VERIFY_SOURCE_OVERLAY`); sealed `false` in tests.
- **`verify.overlay` event** payload `{sha, files, ok}`, emitted once before the overlay `apply_changes`.
- Env prefix `TVASTR_`. Code host owns repo+token (no threading into the verifier).
- Commit footer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`

---

### Task 1: Code-host methods — `get_file_at_ref` + `buggy_parent_sha`

**Files:**
- Modify: `src/tvastr/integrations/github.py` (Protocol `_CodeHostLike` + `MockGitHubClient` + `GitHubClient` + `DryRunCodeHost`)
- Modify: `src/tvastr/agent/context.py` (`CodeHost` Protocol)
- Test: `tests/test_code_host_ref.py` (new)

**Interfaces:**
- Produces:
  - `get_file_at_ref(self, path: str, ref: str) -> str | None`
  - `buggy_parent_sha(self, pr_number: int) -> str | None`
  on the `CodeHost`/`_CodeHostLike` Protocols and all three concrete classes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_code_host_ref.py`:

```python
from __future__ import annotations

from tvastr.integrations.github import DryRunCodeHost, MockGitHubClient


def test_mock_get_file_at_ref_returns_content():
    host = MockGitHubClient("run-llama/llama_index")
    out = host.get_file_at_ref("a/b/base.py", "abc123")
    assert out is not None
    assert "abc123" in out  # ref is reflected so callers can tell versions apart


def test_mock_buggy_parent_sha_is_deterministic():
    host = MockGitHubClient("run-llama/llama_index")
    sha = host.buggy_parent_sha(21447)
    assert sha and isinstance(sha, str)
    assert host.buggy_parent_sha(21447) == sha  # stable


def test_dryrun_delegates_ref_methods():
    inner = MockGitHubClient("run-llama/llama_index")
    host = DryRunCodeHost(inner, "run-llama/llama_index")
    assert host.get_file_at_ref("x/base.py", "deadbeef") == inner.get_file_at_ref(
        "x/base.py", "deadbeef"
    )
    assert host.buggy_parent_sha(10) == inner.buggy_parent_sha(10)
```

- [ ] **Step 2: Run to confirm it fails**

Run: `uv run pytest tests/test_code_host_ref.py -v`
Expected: FAIL — `MockGitHubClient` has no attribute `get_file_at_ref`.

- [ ] **Step 3: Add to the `CodeHost` Protocol (context.py)**

In `src/tvastr/agent/context.py`, inside `class CodeHost(Protocol)`, after `get_file`:

```python
    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        """Return a file's contents at a specific commit/ref, or None."""
        ...

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        """Return the commit just before a PR's fix merged (bug present), or None."""
        ...
```

- [ ] **Step 4: Add to the `_CodeHostLike` Protocol + the three clients (github.py)**

In `src/tvastr/integrations/github.py`:

In `class _CodeHostLike(Protocol)`, after `get_file`:

```python
    def get_file_at_ref(self, path: str, ref: str) -> str | None: ...
    def buggy_parent_sha(self, pr_number: int) -> str | None: ...
```

In `MockGitHubClient`, after `get_file`:

```python
    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        log.info("github.get_file_at_ref", repo=self.repo, path=path, ref=ref, mocked=True)
        return (
            f"# (mock) contents of {path} @ {ref} from {self.repo}\n"
            "def run(self, *args, **kwargs):\n"
            "    ...  # implementation elided in mock mode\n"
        )

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        log.info("github.buggy_parent_sha", repo=self.repo, pr=pr_number, mocked=True)
        return f"buggyparent{pr_number}"
```

In `GitHubClient`, after `get_file`:

```python
    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        try:
            contents = self._get_repo().get_contents(path, ref=ref)
        except Exception as exc:
            log.warning("github.get_file_at_ref.failed", path=path, ref=ref, error=str(exc))
            return None
        if isinstance(contents, list):  # a directory, not a file
            return None
        return contents.decoded_content.decode("utf-8")

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        try:
            repo = self._get_repo()
            pr = repo.get_pull(pr_number)
            merge_sha = pr.merge_commit_sha
            if merge_sha:
                commit = repo.get_commit(merge_sha)
                if commit.parents:
                    return str(commit.parents[0].sha)
            base_sha = getattr(getattr(pr, "base", None), "sha", None)
            return str(base_sha) if base_sha else None
        except Exception as exc:
            log.warning("github.buggy_parent_sha.failed", pr=pr_number, error=str(exc))
            return None
```

In `DryRunCodeHost`, after `get_file`:

```python
    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        return self.inner.get_file_at_ref(path, ref)

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        return self.inner.buggy_parent_sha(pr_number)
```

- [ ] **Step 5: Run tests + lint**

Run: `uv run pytest tests/test_code_host_ref.py -v`
Expected: 3 PASS.
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/integrations/github.py src/tvastr/agent/context.py tests/test_code_host_ref.py
git commit -m "feat(verify): code-host get_file_at_ref + buggy_parent_sha

<footer>"
```

---

### Task 2: Verifier overlay logic + config flag

**Files:**
- Modify: `src/tvastr/config.py` (`verify_source_overlay`)
- Modify: `tests/conftest.py` (seal the flag)
- Modify: `src/tvastr/verification/verifier.py` (`source_overlay` ctor kwarg; `pr_number`/`pr_files` args on `verify()`; `_reproduced`/`_emit_baseline` helpers; the lazy overlay branch; `_apply_buggy_overlay`)
- Test: `tests/test_verifier.py` (overlay scenarios)

**Interfaces:**
- Consumes: `code_host.buggy_parent_sha`, `code_host.get_file_at_ref` (Task 1); `installed_module_path` (sandbox); `handle.apply_changes`; `FileChange`.
- Produces: `Verifier(..., source_overlay: bool = True)`; `verify(..., pr_number: int | None = None, pr_files: list[str] | None = None)`; `verify.overlay` event.

- [ ] **Step 1: Add the config flag**

In `src/tvastr/config.py`, after `verify_provision_deps`:

```python
    # When true, and the released wheel already contains a closed issue's merged
    # fix (baseline returns no_repro), verify overlays the PRE-FIX version of the
    # fixing PR's changed files (fetched at the buggy commit) so the baseline
    # reproduces and the rerun yields a real verdict. Off in tests.
    verify_source_overlay: bool = True
```

- [ ] **Step 2: Seal the flag in conftest**

In `tests/conftest.py`, after the `TVASTR_VERIFY_PROVISION_DEPS` line:

```python
os.environ["TVASTR_VERIFY_SOURCE_OVERLAY"] = "false"
```

- [ ] **Step 3: Write the failing verifier tests**

Add to `tests/test_verifier.py`. Read the file first and reuse its existing helpers (`_FakeHandle`, `_FakeSandbox`, `_ctx`, `_pattern`, `_fix`/`FileChange`, the sink). The `_FakeHandle` already records `applied` and `run_history`; the `_FakeSandbox` exposes `last_handle`. You need a host whose `buggy_parent_sha` + `get_file_at_ref` are scripted — extend the test's context/host helper or add a small fake host. The contract:

```python
def test_overlay_on_no_repro_reproduces_then_verifies():
    # behavioral repro: first baseline prints BEHAVIOR_OK (no_repro), after the
    # buggy overlay the baseline-retry does NOT print it (reproduces), rerun is OK.
    # RunResult sequence (mirror existing behavioral tests for the exact shape):
    #   baseline #1 -> stdout contains BEHAVIOR_OK_MARKER  (no_repro)
    #   baseline #2 (retry, post-overlay) -> stdout WITHOUT marker (reproduced)
    #   rerun -> stdout WITH marker (fixed) -> VERIFIED_VIA_BEHAVIOR
    ...
    verifier = Verifier(_ctx(...), sandbox, event_sink=sink, source_overlay=True)
    result = verifier.verify(
        _pattern(), _root_cause(...), _fix(...), [], "body",
        pr_number=21447, pr_files=["llama-index-integrations/vs/llama-index-vector-stores-postgres/llama_index/vector_stores/postgres/base.py"],
    )
    types = [e.type for e in sink.events]
    assert "verify.overlay" in types
    # apply_changes called twice: buggy overlay, then the agent fix
    assert len(sandbox.last_handle.applied) >= 2
    assert result.verdict in {Verdict.VERIFIED_VIA_BEHAVIOR, Verdict.VERIFIED_VIA_REPRODUCER}


def test_no_overlay_when_baseline_reproduces():
    # first baseline already reproduces -> overlay path never taken.
    verifier = Verifier(_ctx(...), sandbox, event_sink=sink, source_overlay=True)
    verifier.verify(_pattern(), _root_cause(...), _fix(...), [], "body",
                    pr_number=21447, pr_files=["...base.py"])
    assert "verify.overlay" not in [e.type for e in sink.events]


def test_no_overlay_without_pr_number():
    # no_repro but pr_number=None -> stays no_repro, no overlay.
    verifier = Verifier(_ctx(...), sandbox, event_sink=sink, source_overlay=True)
    out = verifier.verify(_pattern(), _root_cause(...), _fix(...), [], "body",
                          pr_number=None, pr_files=None)
    assert "verify.overlay" not in [e.type for e in sink.events]
    assert out.verdict == Verdict.NO_REPRO


def test_overlay_flag_off_skips_overlay():
    verifier = Verifier(_ctx(...), sandbox, event_sink=sink, source_overlay=False)
    out = verifier.verify(_pattern(), _root_cause(...), _fix(...), [], "body",
                          pr_number=21447, pr_files=["...base.py"])
    assert "verify.overlay" not in [e.type for e in sink.events]
    assert out.verdict == Verdict.NO_REPRO
```

> Implementer note: read `tests/test_verifier.py` and match its exact `_ctx`,
> `_pattern`, `_root_cause`, `_fix`, sink, and `RunResult` conventions. The
> behavioral-repro path keys on `BEHAVIOR_OK_MARKER` in stdout — use the same
> `_ctx(_BEHAVIORAL_REPRO)`-style scripting the existing behavioral tests use so
> the synthesized reproducer is behavioral. The assertions above are the binding
> contract. The fake host must script `buggy_parent_sha` → a sha and
> `get_file_at_ref` → some content; if the existing `_ctx` host is a
> `MockGitHubClient`, its defaults already satisfy this (non-None sha + content).

- [ ] **Step 4: Run to confirm failure**

Run: `uv run pytest tests/test_verifier.py -k overlay -v`
Expected: FAIL — `verify()` has no `pr_number`/`pr_files`; no `verify.overlay`.

- [ ] **Step 5: Add the ctor kwarg + verify() args + helpers**

In `src/tvastr/verification/verifier.py`:

Extend the sandbox import (currently `from tvastr.verification.sandbox import Sandbox, distribution_for_path`):

```python
from tvastr.verification.sandbox import Sandbox, distribution_for_path, installed_module_path
```

In `__init__`, add the kwarg (after `provision_deps`):

```python
        provision_deps: bool = True,
        source_overlay: bool = True,
    ) -> None:
        ...
        self.provision_deps = provision_deps
        self.source_overlay = source_overlay
```

Add two helpers (near `_emit`):

```python
    def _reproduced(self, result: RunResult, repro: Reproducer) -> bool:
        if repro.kind == ReproducerKind.BEHAVIORAL:
            return BEHAVIOR_OK_MARKER not in result.stdout
        return _original_exception_seen(result, repro.expected_exception) or not result.succeeded

    def _emit_baseline(self, result: RunResult, repro: Reproducer, *, retry: bool = False) -> None:
        self._emit(
            "verify.baseline",
            "verify",
            {
                "exit_code": result.exit_code,
                "stderr_tail": _tail(result.stderr),
                "original_exception_seen": _original_exception_seen(result, repro.expected_exception),
                "timed_out": result.timed_out,
                "retry": retry,
            },
        )

    def _apply_buggy_overlay(
        self, handle, pr_number: int, pr_files: list[str]
    ) -> list[str]:
        """Overlay the pre-fix version of the PR's changed files; return paths applied."""
        sha = self.ctx.code_host.buggy_parent_sha(pr_number)
        if not sha:
            return []
        changes: list[FileChange] = []
        for path in pr_files:
            if installed_module_path(path) is None:
                continue  # skip docs/tests/notebooks the PR also touched
            content = self.ctx.code_host.get_file_at_ref(path, sha)
            if content is not None:
                changes.append(
                    FileChange(path=path, patched_content=content, rationale="buggy overlay (pre-fix)")
                )
        if not changes:
            return []
        self._emit(
            "verify.overlay",
            "verify",
            {"sha": sha, "files": [c.path for c in changes], "ok": True},
        )
        handle.apply_changes(changes)
        return [c.path for c in changes]
```

Update `verify()`'s signature:

```python
    def verify(
        self,
        pattern: FailurePattern,
        root_cause: RootCause,
        fix: FixProposal,
        sample_events: list[LogEvent],
        issue_body: str | None,
        pr_number: int | None = None,
        pr_files: list[str] | None = None,
    ) -> VerificationResult:
```

- [ ] **Step 6: Rewrite the baseline / no_repro block to use the helpers + overlay**

Replace the existing block (the `self._emit("verify.baseline", ...)` call through the `if not baseline_reproduced: return self._finish(... NO_REPRO ...)`) with:

```python
            handle.write_file("repro.py", repro.code)
            baseline: RunResult = handle.run(["python", "repro.py"], timeout_s=90)
            self._emit_baseline(baseline, repro)
            baseline_reproduced = self._reproduced(baseline, repro)

            if not baseline_reproduced and self.source_overlay and pr_number:
                # Released wheel already carries the fix (no_repro). Reconstruct
                # the pre-fix state of the PR's changed files and re-baseline.
                try:
                    overlaid = self._apply_buggy_overlay(handle, pr_number, pr_files or [])
                except Exception as exc:  # overlay must never abort verify
                    overlaid = []
                    log.warning("verify.overlay.error", error=str(exc))
                if overlaid:
                    baseline = handle.run(["python", "repro.py"], timeout_s=90)
                    self._emit_baseline(baseline, repro, retry=True)
                    baseline_reproduced = self._reproduced(baseline, repro)

            if not baseline_reproduced:
                # Reproducer ran cleanly without ever hitting the bug: we have
                # no signal to evaluate "is the fix necessary?" — be honest.
                return self._finish(
                    handle,
                    Verdict.NO_REPRO,
                    "none",
                    started,
                    {"reason": "baseline run did not reproduce the failure"},
                )
```

Keep the existing `# 4. Apply patch` / `handle.apply_changes(fix.changes)` block and everything after it unchanged.

- [ ] **Step 7: Run the verifier tests**

Run: `uv run pytest tests/test_verifier.py -v`
Expected: all PASS (new overlay tests + existing).

- [ ] **Step 8: Full suite + lint**

Run: `uv run pytest -q`
Expected: all green (report count).
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/config.py tests/conftest.py src/tvastr/verification/verifier.py tests/test_verifier.py
git commit -m "feat(verify): buggy-file overlay on no_repro (source-at-buggy-commit)

<footer>"
```

---

### Task 3: Route reconstruction — thread `pr_number` + `pr_files` into verify

**Files:**
- Modify: `src/tvastr/api/routes/verify.py` (`_reconstruct_from_run`, `_run_in_thread`, `verify_run`)
- Test: `tests/test_verify_route_reconstruct.py` (new) — if a route/reconstruct test module exists, add there instead.

**Interfaces:**
- Consumes: `Verifier(..., source_overlay=)`, `verify(..., pr_number=, pr_files=)` (Task 2); `benchmark.compared` event payload (`pr_number`, `files_both`, `files_theirs_only`).
- Produces: `_reconstruct_from_run` returns `pr_number` + `pr_files`; the verifier is constructed with `source_overlay=settings.verify_source_overlay` and `verify()` is called with `pr_number`/`pr_files`.

- [ ] **Step 1: Write the failing reconstruct test**

Create `tests/test_verify_route_reconstruct.py`. Build a persisted run with the minimal events `_reconstruct_from_run` needs (`pipeline.start`, `detect.cluster` or `agent.start`, `fix.generated`, `benchmark.compared`) using the project's `JsonlEventSink`/`run_path` + `PipelineEvent`, then assert the reconstructed `pr_number`/`pr_files`:

```python
from __future__ import annotations

from tvastr.api.routes.verify import _reconstruct_from_run
from tvastr.events import JsonlEventSink, PipelineEvent
from tvastr.runlog import run_path  # adjust import to where run_path lives


def _emit(sink, type_, payload):
    sink.emit(PipelineEvent(type=type_, layer="output", step="t", run_id="recon-test", payload=payload))


def test_reconstruct_extracts_pr_number_and_files(tmp_path, monkeypatch):
    # Point run storage at tmp if needed; otherwise use a unique run_id.
    rid = "recon-test"
    sink = JsonlEventSink(run_path(rid))
    _emit(sink, "pipeline.start", {"repo": "run-llama/llama_index", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(sink, "fix.generated", {"patched_files": {"a/llama_index/x/base.py": "# fix"}, "summary": "s"})
    _emit(sink, "benchmark.compared", {
        "pr_number": 21447,
        "files_both": ["a/llama_index/x/base.py"],
        "files_theirs_only": ["a/llama_index/x/util.py"],
    })
    out = _reconstruct_from_run(rid)
    assert out is not None
    *_, pr_number, pr_files = out
    assert pr_number == 21447
    assert pr_files == ["a/llama_index/x/base.py", "a/llama_index/x/util.py"]
```

> Implementer note: confirm the real import path of `run_path`/`load_events`
> (grep `def run_path`), and whether runs write under a configurable dir you must
> point at `tmp_path`. Match how existing route tests set up persisted runs. If
> the suite already has a verify-route test module, add this test there.

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_verify_route_reconstruct.py -v`
Expected: FAIL — reconstruct returns a 6-tuple; unpacking `*_, pr_number, pr_files` is wrong / values absent.

- [ ] **Step 3: Extend `_reconstruct_from_run`**

In `src/tvastr/api/routes/verify.py`:

Update the type alias:

```python
_Reconstructed = tuple[
    FailurePattern, RootCause, FixProposal, list[LogEvent], str, str | None, int | None, list[str]
]
```

Add accumulators near the other locals in `_reconstruct_from_run`:

```python
    pr_number: int | None = None
    pr_files: list[str] = []
```

Add a branch in the event loop (alongside the others):

```python
        if ev.type == "benchmark.compared":
            raw_pr = p.get("pr_number")
            if raw_pr is not None:
                try:
                    pr_number = int(raw_pr)
                except (TypeError, ValueError):
                    pr_number = None
            pr_files = list(p.get("files_both") or []) + list(p.get("files_theirs_only") or [])
```

Update the return:

```python
    return pattern, root_cause, fix, sample_events, repo, issue_title_str, pr_number, pr_files
```

- [ ] **Step 4: Thread through `_run_in_thread` + `verify_run`**

In `_run_in_thread`, add params (after `issue_body`):

```python
    issue_body: str | None,
    pr_number: int | None,
    pr_files: list[str],
    sink: EventSink,
    q: queue.Queue[Any],
) -> threading.Thread:
```

Pass `source_overlay` to the `Verifier` ctor:

```python
    verifier = Verifier(
        ctx,
        sandbox,
        project_root=project_root,
        event_sink=sink,
        run_id=run_id,
        provision_deps=settings.verify_provision_deps,
        source_overlay=settings.verify_source_overlay,
    )
```

Pass the new args to `verify()`:

```python
            verifier.verify(
                pattern, root_cause, fix, sample_events, issue_body,
                pr_number=pr_number, pr_files=pr_files,
            )
```

In `verify_run`, update the unpack + the `_run_in_thread` call:

```python
    pattern, root_cause, fix, sample_events, _repo, _issue_title, pr_number, pr_files = reconstructed
    ...
    _run_in_thread(
        run_id=run_id,
        pattern=pattern,
        root_cause=root_cause,
        fix=fix,
        sample_events=sample_events,
        issue_body=request.issue_body,
        pr_number=pr_number,
        pr_files=pr_files,
        sink=...,   # keep the existing sink/q args as they are
        q=...,
    )
```

(Keep the existing `sink=`/`q=` arguments to `_run_in_thread` exactly as they were.)

- [ ] **Step 5: Run the reconstruct test + full suite + lint**

Run: `uv run pytest tests/test_verify_route_reconstruct.py -v`
Expected: PASS.
Run: `uv run pytest -q`
Expected: all green (report count).
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/api/routes/verify.py tests/test_verify_route_reconstruct.py
git commit -m "feat(verify): reconstruct pr_number+pr_files and feed the overlay

<footer>"
```

---

### Task 4: Render `verify.overlay` in the dashboard

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (verify allowlist + summary switch)

**Interfaces:**
- Consumes: the `verify.overlay` event (payload `{sha, files, ok}`).

- [ ] **Step 1: Add to the verify event allowlist**

In `src/tvastr/api/templates/app.html`, the verify allowlist currently reads:

```javascript
  "verify.start","verify.provision","verify.repro_synth","verify.baseline",
  "verify.patch_applied","verify.rerun","verify.regression","verify.result"
```

Add `"verify.overlay"` right after `"verify.baseline"`:

```javascript
  "verify.start","verify.provision","verify.repro_synth","verify.baseline","verify.overlay",
  "verify.patch_applied","verify.rerun","verify.regression","verify.result"
```

- [ ] **Step 2: Add a summary label**

In the summary switch, after the `case "verify.baseline":` line, add:

```javascript
    case "verify.overlay": return `buggy overlay @ ${(p.sha||"").slice(0,8)} · ${(p.files||[]).length} file(s)${p.ok?"":" · FAILED"}`;
```

- [ ] **Step 3: Verify the page parses + count the literal**

Run: `uv run python -c "from pathlib import Path; import tvastr.api.app as a; html=(Path(a.__file__).resolve().parent/'templates'/'app.html').read_text(); assert html.count('verify.overlay')==2; print('ok: 2')"`
Expected: `ok: 2`

- [ ] **Step 4: Suite + lint (sanity)**

Run: `uv run pytest -q`  → green.
Run: `uv run ruff check src tests`  → `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(verify): surface verify.overlay in the dashboard timeline

<footer>"
```

---

## Self-Review

**Spec coverage:**
- Buggy-file overlay at SHA → Task 2 (`_apply_buggy_overlay`) + Task 1 (code-host methods). ✓
- Buggy SHA = merge_commit parent, fallback base.sha → Task 1 `buggy_parent_sha`. ✓
- Lazy trigger (only after no_repro, flag on, pr_number known) → Task 2 Step 6. ✓
- Reuse `apply_changes` → Task 2 `_apply_buggy_overlay` calls `handle.apply_changes`. ✓
- `pr_files` from `benchmark.compared` (`files_both`+`files_theirs_only`) → Task 3. ✓
- Scope filter via `installed_module_path` → Task 2 `_apply_buggy_overlay`. ✓
- Never crash / degrade to no_repro → Task 2 try/except + final no_repro return. ✓
- Config flag + conftest seal → Task 2 Steps 1-2. ✓
- `verify.overlay` event + UI → Task 2 + Task 4. ✓
- Both Protocols updated → Task 1 Steps 3-4. ✓

**Placeholder scan:** Tasks 2 & 3 leave test scaffolding (`_ctx(...)`, `RunResult` sequence, `run_path` import) to match the existing test files — the implementer must read those files first; the assertions are concrete and binding. All production code blocks are complete. `<footer>` in commit messages = the two-line co-author/session footer from Global Constraints.

**Type consistency:** `get_file_at_ref(path, ref) -> str | None` and `buggy_parent_sha(pr_number) -> str | None` identical across both Protocols + 3 clients + verifier calls. `source_overlay` consistent (config `verify_source_overlay` → ctor `source_overlay` → `settings.verify_source_overlay`). `verify(..., pr_number, pr_files)` consistent across verifier + route. `_Reconstructed` 8-tuple matches the unpack in `verify_run`.
