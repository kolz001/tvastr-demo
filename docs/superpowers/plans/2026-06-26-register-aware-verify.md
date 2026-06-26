# Register-Aware Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the verify loop judge a fix by the success definition of *its register* (REPAIR/FAIL_FAST behaviorally, WARN by warning-emitted, BETTER_ERROR by error-clarity, DOCUMENT not behaviorally), so a maintainer-correct non-behavioral fix isn't wrongly marked `STILL_BROKEN`; and derive the overlay base only from merged-to-`main` fixes.

**Architecture:** The register is carried on `FixProposal.register` (default `REPAIR` → today's behavior, zero regression). It is realized through the *reproducer's assertion* (register-aware synthesis), reusing the entire baseline→rerun→verdict flow — WARN/BETTER_ERROR repros are behavioral (assert + `BEHAVIOR_OK_MARKER`), so only the synthesized assertion and the green verdict *label* change. `DOCUMENT` short-circuits before sandboxing. The buggy-overlay base is gated to merged PRs by dropping `buggy_parent_sha`'s `base.sha` fallback.

**Tech Stack:** Python 3.11/3.12, pydantic `BaseModel` domain types, `StrEnum`, the LangGraph-style agent, Docker/subprocess verify sandbox, `uv`, `ruff`, `pytest`.

## Global Constraints

- **Zero regression for `REPAIR`:** `FixProposal.register` defaults to `FixRegister.REPAIR`; the current `generate_fix` path never sets it, so verify behaves exactly as today until the judgment plan labels fixes.
- **Verify trusts the label** — no independent diff re-classification in this plan.
- **Never crash:** missing/unknown register → `REPAIR`; `DOCUMENT` short-circuits cleanly; unmerged PR → no overlay (honest `no_repro`). No new crash surface.
- **`repro.py` runs as `python repro.py`** — synthesized reproducers use stdlib only (`warnings.catch_warnings`, plain `try/except`); no `pytest` import in the reproducer.
- **WARN/BETTER_ERROR reproducers are BEHAVIORAL** (assert the register signal, end with `print("<BEHAVIOR_OK_MARKER>")`), so they reuse the existing behavioral green path.
- **Merged-to-`main` base only:** `buggy_parent_sha` returns `None` for an unmerged PR (no `merge_commit_sha`); drop the `base.sha` fallback (it resolves to current `main` = already-fixed).
- **`FixRegister` lives in `tvastr.domain`** (so `FixProposal` uses it and the future judgment plan imports it; do not redefine it elsewhere).
- All LLM calls stay on `ctx.router.run(...)`. `ruff` clean. TDD: failing test first.
- Commit footer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`

---

### Task 1: Foundation — `FixRegister`, `FixProposal.register`, new verdicts

**Files:**
- Modify: `src/tvastr/domain/models.py` (add `FixRegister`; add `register` to `FixProposal`)
- Modify: `src/tvastr/domain/__init__.py` (export `FixRegister`)
- Modify: `src/tvastr/verification/models.py` (add 3 `Verdict` members; extend `is_green`)
- Test: `tests/test_register_foundation.py` (new)

**Interfaces:**
- Produces: `class FixRegister(StrEnum)` = `REPAIR="repair"`, `FAIL_FAST="fail_fast"`, `WARN="warn"`, `BETTER_ERROR="better_error"`, `DOCUMENT="document"`. `FixProposal.register: FixRegister = FixRegister.REPAIR`. `Verdict.VERIFIED_VIA_WARNING`, `Verdict.VERIFIED_VIA_BETTER_ERROR`, `Verdict.UNVERIFIED_DOC_ONLY`. The two `VERIFIED_VIA_*` are in `VerificationResult.is_green`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_register_foundation.py
from tvastr.domain import FixProposal, FixRegister
from tvastr.verification.models import Verdict, VerificationResult


def test_fix_register_values():
    assert FixRegister.REPAIR == "repair"
    assert {r.value for r in FixRegister} == {
        "repair", "fail_fast", "warn", "better_error", "document"
    }


def test_fix_proposal_defaults_to_repair():
    fp = FixProposal(pattern_id="p", summary="s")
    assert fp.register == FixRegister.REPAIR


def test_fix_proposal_accepts_register():
    fp = FixProposal(pattern_id="p", summary="s", register=FixRegister.WARN)
    assert fp.register == FixRegister.WARN


def test_new_verdicts_exist_and_greenness():
    assert Verdict.VERIFIED_VIA_WARNING == "verified_via_warning"
    assert Verdict.VERIFIED_VIA_BETTER_ERROR == "verified_via_better_error"
    assert Verdict.UNVERIFIED_DOC_ONLY == "unverified_doc_only"
    green = VerificationResult(verdict=Verdict.VERIFIED_VIA_WARNING, oracle="warning", elapsed_s=1.0)
    assert green.is_green
    assert VerificationResult(
        verdict=Verdict.VERIFIED_VIA_BETTER_ERROR, oracle="better_error", elapsed_s=1.0
    ).is_green
    assert not VerificationResult(
        verdict=Verdict.UNVERIFIED_DOC_ONLY, oracle="none", elapsed_s=1.0
    ).is_green
```

- [ ] **Step 2: Run, watch fail** — `uv run pytest tests/test_register_foundation.py -v` → FAIL (`FixRegister` import error).

- [ ] **Step 3: Add `FixRegister` + `register` field in `domain/models.py`**

After the existing `class Sensitivity(StrEnum):` block (near the other StrEnums, ~line 40), add:

```python
class FixRegister(StrEnum):
    """The *response register* of a fix — how it addresses the failure.

    REPAIR/FAIL_FAST change behavior; WARN/BETTER_ERROR surface it; DOCUMENT only
    informs humans. Verify judges each register by its own success definition.
    """

    REPAIR = "repair"
    FAIL_FAST = "fail_fast"
    WARN = "warn"
    BETTER_ERROR = "better_error"
    DOCUMENT = "document"
```

In `class FixProposal(BaseModel):`, add the field (keep the others unchanged):

```python
class FixProposal(BaseModel):
    """A concrete, reviewable change set that addresses a root cause."""

    pattern_id: str
    summary: str
    changes: list[FileChange] = Field(default_factory=list)
    test_plan: str = ""
    register: FixRegister = FixRegister.REPAIR
```

- [ ] **Step 4: Export `FixRegister` from `domain/__init__.py`**

In `src/tvastr/domain/__init__.py`, add `FixRegister` to both the import from `.models` and `__all__` (alphabetically near `FixProposal`):

```python
from .models import (
    ...
    FixProposal,
    FixRegister,
    ...
)

__all__ = [
    ...
    "FixProposal",
    "FixRegister",
    ...
]
```

- [ ] **Step 5: Add the verdicts in `verification/models.py`**

In `class Verdict(StrEnum):`, after `VERIFIED_VIA_BEHAVIOR`, add:

```python
    VERIFIED_VIA_WARNING = "verified_via_warning"
    VERIFIED_VIA_BETTER_ERROR = "verified_via_better_error"
```

and after `UNVERIFIED_SMOKE_IMPORT_ONLY`, add:

```python
    UNVERIFIED_DOC_ONLY = "unverified_doc_only"
```

In `VerificationResult.is_green`, add the two new green verdicts to the set:

```python
    @property
    def is_green(self) -> bool:
        return self.verdict in {
            Verdict.VERIFIED_VIA_REPRODUCER,
            Verdict.VERIFIED_VIA_SCOPED_TESTS,
            Verdict.VERIFIED_VIA_BEHAVIOR,
            Verdict.VERIFIED_VIA_WARNING,
            Verdict.VERIFIED_VIA_BETTER_ERROR,
        }
```

- [ ] **Step 6: Run, pass** — `uv run pytest tests/test_register_foundation.py -v` → PASS. Then `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/domain/models.py src/tvastr/domain/__init__.py src/tvastr/verification/models.py tests/test_register_foundation.py
git commit -m "feat(verify): FixRegister + FixProposal.register + register verdicts

<footer>"
```

---

### Task 2: Register-aware reproducer synthesis

**Files:**
- Modify: `src/tvastr/verification/repro.py` (`synthesize_reproducer` + `_build_prompt` gain `register`)
- Test: `tests/test_repro_register.py` (new)

**Interfaces:**
- Consumes: `FixRegister` (Task 1).
- Produces: `synthesize_reproducer(..., register: FixRegister = FixRegister.REPAIR)`; `_build_prompt(..., register: FixRegister = FixRegister.REPAIR)` appends register-specific assertion guidance for `WARN`/`BETTER_ERROR`; `REPAIR`/`FAIL_FAST` prompts are unchanged.

- [ ] **Step 1: Write the failing test** (offline — asserts the *prompt* carries register guidance; output content depends on the LLM and is not asserted)

```python
# tests/test_repro_register.py
from tvastr.domain import FixRegister
from tvastr.verification.repro import _build_prompt
from tvastr.domain.models import FailurePattern, RootCause, Sensitivity


def _pat():
    return FailurePattern(fingerprint="f", title="oversized _node_content",
                          representative_message="metadata too large", exception_type=None,
                          count=1, sensitivity=Sensitivity.INTERNAL)


def _rc():
    return RootCause(pattern_id="f", summary="_node_content exceeds the filter limit",
                     suspected_files=["base.py"], confidence=0.8)


def test_warn_prompt_requests_warning_assertion():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.WARN)
    assert "catch_warnings" in p
    assert "warning" in p.lower()


def test_better_error_prompt_requests_error_assertion():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.BETTER_ERROR)
    assert "try" in p and "except" in p
    assert "error" in p.lower()


def test_repair_prompt_has_no_register_section():
    p = _build_prompt(_pat(), _rc(), [], register=FixRegister.REPAIR)
    assert "catch_warnings" not in p
```

- [ ] **Step 2: Run, watch fail** — `uv run pytest tests/test_repro_register.py -v` → FAIL (`_build_prompt` has no `register`).

- [ ] **Step 3: Implement register-aware prompt**

In `src/tvastr/verification/repro.py`, add the import:

```python
from tvastr.domain import FixRegister
```

Add a guidance map near `_SYSTEM`:

```python
_REGISTER_GUIDANCE: dict[FixRegister, str] = {
    FixRegister.WARN: (
        "\n\nFIX REGISTER = WARN: the fix does NOT change behavior; it emits a "
        "warning on the failing input. Write a BEHAVIORAL reproducer that triggers "
        "the failing input INSIDE:\n"
        "    import warnings\n"
        "    with warnings.catch_warnings(record=True) as _w:\n"
        "        warnings.simplefilter('always')\n"
        "        <trigger the failing input>\n"
        "then assert at least one captured warning's message references the failing "
        "symbol/field, and end with the marker. The bug = NO such warning at baseline."
    ),
    FixRegister.BETTER_ERROR: (
        "\n\nFIX REGISTER = BETTER_ERROR: the fix replaces a cryptic failure with a "
        "clearer error. Write a BEHAVIORAL reproducer that triggers the failing input "
        "inside try/except, asserts the raised error's type or message references the "
        "failing symbol/field (the clearer error), and ends with the marker. Use stdlib "
        "only — no pytest."
    ),
}
```

Change `_build_prompt`'s signature and append the guidance:

```python
def _build_prompt(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    code_context: str = "",
    register: FixRegister = FixRegister.REPAIR,
) -> str:
    ...  # body unchanged up to the final return
    base = (
        f"Failure title: {pattern.title}\n"
        ...  # everything exactly as today
        "Write the reproducer."
    )
    return base + _REGISTER_GUIDANCE.get(register, "")
```

(Keep the existing return string verbatim as `base`, then append the guidance.)

Change `synthesize_reproducer` to accept and forward `register`:

```python
def synthesize_reproducer(
    pattern: FailurePattern,
    root_cause: RootCause,
    sample_events: list[LogEvent],
    issue_body: str | None,
    router: HybridRouter,
    code_context: str | Callable[[], str] = "",
    register: FixRegister = FixRegister.REPAIR,
) -> Reproducer:
    ...
    prompt = _build_prompt(
        pattern, root_cause, sample_events, code_context=_truncate(resolved_context),
        register=register,
    )
    ...
```

(The issue-body extraction path stays register-agnostic — a runnable block in the issue body is used as-is.)

- [ ] **Step 4: Run, pass** — `uv run pytest tests/test_repro_register.py tests/test_verifier.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/verification/repro.py tests/test_repro_register.py
git commit -m "feat(verify): register-aware reproducer synthesis (warn / better_error)

<footer>"
```

---

### Task 3: Verifier — thread register, DOCUMENT short-circuit, verdict mapping

**Files:**
- Modify: `src/tvastr/verification/verifier.py`
- Test: `tests/test_verifier.py` (add register cases)

**Interfaces:**
- Consumes: `FixRegister` (Task 1); register-aware `synthesize_reproducer` (Task 2); `fix.register`.
- Produces: `DOCUMENT` fix → `UNVERIFIED_DOC_ONLY` (no sandbox); a behavioral-green `WARN` fix → `VERIFIED_VIA_WARNING`; `BETTER_ERROR` → `VERIFIED_VIA_BETTER_ERROR`; `REPAIR`/`FAIL_FAST` unchanged.

- [ ] **Step 1: Write the failing tests** (reuse the file's `_FakeSandbox`/`_ctx`/`_pattern`/`_root_cause`/`_fix` helpers; read the file first)

```python
# add to tests/test_verifier.py
from tvastr.domain import FixRegister

def _fix_reg(register):
    f = _fix()
    return f.model_copy(update={"register": register})

def test_document_register_short_circuits_unverified_doc_only():
    sandbox = _FakeSandbox([])  # must never be used
    verifier = Verifier(_ctx(), sandbox)
    out = verifier.verify(_pattern(), _root_cause(), _fix_reg(FixRegister.DOCUMENT),
                          [_event()], issue_body=None)
    assert out.verdict == Verdict.UNVERIFIED_DOC_ONLY
    assert sandbox.last_handle is None  # never prepared a sandbox

def test_warn_register_greens_via_warning(monkeypatch):
    # behavioral repro: baseline fails (no warning), rerun passes (warning fires)
    import tvastr.verification.verifier as vmod
    monkeypatch.setattr(vmod, "synthesize_reproducer", lambda *a, **k:
        vmod.Reproducer(source=vmod.ReproducerSource.CLAUDE,
                        code="x", kind=vmod.ReproducerKind.BEHAVIORAL))
    sandbox = _FakeSandbox([
        RunResult(exit_code=1, stdout="", stderr="AssertionError"),                 # baseline: no warning
        RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),        # rerun: warning fires
    ])
    out = Verifier(_ctx(), sandbox).verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.WARN), [_event()], issue_body=None)
    assert out.verdict == Verdict.VERIFIED_VIA_WARNING
    assert out.oracle == "warning"

def test_better_error_register_greens_via_better_error(monkeypatch):
    import tvastr.verification.verifier as vmod
    monkeypatch.setattr(vmod, "synthesize_reproducer", lambda *a, **k:
        vmod.Reproducer(source=vmod.ReproducerSource.CLAUDE,
                        code="x", kind=vmod.ReproducerKind.BEHAVIORAL))
    sandbox = _FakeSandbox([
        RunResult(exit_code=1, stdout="", stderr="AssertionError"),
        RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
    ])
    out = Verifier(_ctx(), sandbox).verify(
        _pattern(), _root_cause(), _fix_reg(FixRegister.BETTER_ERROR), [_event()], issue_body=None)
    assert out.verdict == Verdict.VERIFIED_VIA_BETTER_ERROR
```

> Implementer note: confirm `Reproducer`, `ReproducerSource`, `ReproducerKind` are importable from `tvastr.verification.verifier` (it imports them); if not, import from `tvastr.verification.models` in the test. Match the existing tests' construction of `_FakeSandbox`/`RunResult`.

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement** in `src/tvastr/verification/verifier.py`:

Add imports + maps near the top:

```python
from tvastr.domain import FailurePattern, FileChange, FixProposal, FixRegister, LogEvent, RootCause
```
(extend the existing `from tvastr.domain import ...` line with `FixRegister`.)

After `_original_exception_seen` (module level), add:

```python
_REGISTER_GREEN: dict[FixRegister, tuple[Verdict, str]] = {
    FixRegister.WARN: (Verdict.VERIFIED_VIA_WARNING, "warning"),
    FixRegister.BETTER_ERROR: (Verdict.VERIFIED_VIA_BETTER_ERROR, "better_error"),
}
```

In `verify()`, right after the `verify.start` emit and before the reproducer block, add the DOCUMENT short-circuit:

```python
        if fix.register == FixRegister.DOCUMENT:
            # A docs-only fix has no runtime signal — be honest rather than
            # running a behavioral oracle it can never satisfy.
            return self._fail(
                Verdict.UNVERIFIED_DOC_ONLY,
                "none",
                started,
                {"reason": "documentation-only fix; no behavioral oracle applies"},
            )
```

Forward the register into synthesis (in the `synthesize_reproducer(...)` call):

```python
            repro: Reproducer = synthesize_reproducer(
                pattern,
                root_cause,
                sample_events,
                issue_body,
                self.ctx.router,
                code_context=_code_context,
                register=fix.register,
            )
```

At the behavioral green point (currently sets `green_verdict = Verdict.VERIFIED_VIA_BEHAVIOR; green_oracle = "behavior"`), map by register:

```python
                if rerun.exit_code == 0 and BEHAVIOR_OK_MARKER in rerun.stdout:
                    green_verdict, green_oracle = _REGISTER_GREEN.get(
                        fix.register, (Verdict.VERIFIED_VIA_BEHAVIOR, "behavior")
                    )
```

(The crash-repro green path — `VERIFIED_VIA_REPRODUCER` — is unchanged: only REPAIR/FAIL_FAST produce crash repros.)

Add `register` to the `verify.repro_synth` payload so the timeline shows it:

```python
            {"source": repro.source.value, "code": repro.code,
             "expected_exception": repro.expected_exception, "register": fix.register.value},
```

- [ ] **Step 4: Run, pass** — `uv run pytest tests/test_verifier.py -v` → PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/verification/verifier.py tests/test_verifier.py
git commit -m "feat(verify): register-aware oracle + DOCUMENT short-circuit

<footer>"
```

---

### Task 4: Merged-to-`main` base — `buggy_parent_sha`

**Files:**
- Modify: `src/tvastr/integrations/github.py` (`GitHubClient.buggy_parent_sha`)
- Test: `tests/test_buggy_parent_merged.py` (new)

**Interfaces:**
- Produces: `GitHubClient.buggy_parent_sha(pr_number)` returns the `merge_commit_sha`'s first parent for a MERGED PR, and `None` for an unmerged PR (no `base.sha` fallback).

- [ ] **Step 1: Write the failing test** (monkeypatch `_get_repo` → no network)

```python
# tests/test_buggy_parent_merged.py
from tvastr.integrations.github import GitHubClient


class _Parent:
    sha = "parentsha"

class _MergedPR:
    merge_commit_sha = "mergesha"

class _UnmergedPR:
    merge_commit_sha = None
    base = type("B", (), {"sha": "current-main-sha"})()

class _Commit:
    parents = [_Parent()]


def test_merged_pr_returns_merge_parent(monkeypatch):
    c = GitHubClient(token="x", repo="run-llama/llama_index")
    class _Repo:
        def get_pull(self, n): return _MergedPR()
        def get_commit(self, s): return _Commit()
    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    assert c.buggy_parent_sha(123) == "parentsha"


def test_unmerged_pr_returns_none(monkeypatch):
    c = GitHubClient(token="x", repo="run-llama/llama_index")
    class _Repo:
        def get_pull(self, n): return _UnmergedPR()
    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    # No base.sha fallback: an unmerged PR has no valid buggy base on main.
    assert c.buggy_parent_sha(123) is None
```

- [ ] **Step 2: Run, watch fail** — the current code falls back to `base.sha` ("current-main-sha"), so `test_unmerged_pr_returns_none` FAILS.

- [ ] **Step 3: Implement** — replace `GitHubClient.buggy_parent_sha` body:

```python
    def buggy_parent_sha(self, pr_number: int) -> str | None:
        # Merged-to-main only: the buggy base is the merge commit's first parent
        # (mainline immediately before the fix). An unmerged PR has no merge
        # commit and no valid buggy base — base.sha would be the CURRENT main tip
        # (already fixed), so we return None rather than fall back to it.
        try:
            repo = self._get_repo()
            pr = repo.get_pull(pr_number)
            merge_sha = pr.merge_commit_sha
            if not merge_sha:
                return None
            commit = repo.get_commit(merge_sha)
            return str(commit.parents[0].sha) if commit.parents else None
        except Exception as exc:
            log.warning("github.buggy_parent_sha.failed", pr=pr_number, error=str(exc))
            return None
```

- [ ] **Step 4: Run, pass** — `uv run pytest tests/test_buggy_parent_merged.py tests/test_code_host_ref.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/integrations/github.py tests/test_buggy_parent_merged.py
git commit -m "fix(verify): buggy_parent_sha is merged-only (drop base.sha fallback)

<footer>"
```

---

### Task 5: Persist + reconstruct the register

**Files:**
- Modify: `src/tvastr/agent/graph.py` (`_generate_fix` emits `register`)
- Modify: `src/tvastr/api/routes/verify.py` (`_reconstruct_from_run` rebuilds `FixProposal.register`)
- Test: `tests/test_verify_route_reconstruct.py` (add a register case)

**Interfaces:**
- Consumes: `FixProposal.register` (Task 1).
- Produces: the `fix.generated` event payload carries `register`; `_reconstruct_from_run` sets `FixProposal(register=...)` (default `REPAIR` when absent/invalid).

- [ ] **Step 1: Write the failing test** (extend the existing reconstruct test module; match its run-building helpers)

```python
# add to tests/test_verify_route_reconstruct.py
from tvastr.domain import FixRegister

def test_reconstruct_reads_register(tmp_path, monkeypatch):
    rid = "recon-register"
    sink = JsonlEventSink(run_path(rid))   # match the module's existing setup
    _emit(sink, "pipeline.start", {"repo": "run-llama/llama_index", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(sink, "fix.generated", {"patched_files": {"a/llama_index/x/base.py": "# f"},
                                  "summary": "s", "register": "warn"})
    pattern, root_cause, fix, *_ = _reconstruct_from_run(rid)
    assert fix.register == FixRegister.WARN

def test_reconstruct_register_defaults_repair(tmp_path):
    rid = "recon-register-default"
    sink = JsonlEventSink(run_path(rid))
    _emit(sink, "pipeline.start", {"repo": "r", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(sink, "fix.generated", {"patched_files": {"a/llama_index/x/base.py": "# f"}, "summary": "s"})
    _, _, fix, *_ = _reconstruct_from_run(rid)
    assert fix.register == FixRegister.REPAIR
```

- [ ] **Step 2: Run, watch fail** — `fix.register` is always `REPAIR` (payload ignored) → `test_reconstruct_reads_register` FAILS.

- [ ] **Step 3a: Emit `register` from the graph**

In `src/tvastr/agent/graph.py` `_generate_fix`, add to the `fix.generated` emit (after `rationales=...`):

```python
            rationales={c.path: c.rationale for c in fix.changes},
            register=fix.register.value,
```

- [ ] **Step 3b: Reconstruct `register`**

In `src/tvastr/api/routes/verify.py`, import `FixRegister`:

```python
from tvastr.domain import FixRegister
```

In `_reconstruct_from_run`, where the `FixProposal(...)` is built from the `fix.generated` event, add the `register` kwarg with a safe parse:

```python
            try:
                register = FixRegister(p.get("register", "repair"))
            except ValueError:
                register = FixRegister.REPAIR
            fix = FixProposal(
                pattern_id=pattern.id if pattern else "",
                summary=str(p.get("summary", "")),
                changes=file_changes,
                test_plan=str(p.get("test_plan", "")),
                register=register,
            )
```

- [ ] **Step 4: Run, pass** — `uv run pytest tests/test_verify_route_reconstruct.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/agent/graph.py src/tvastr/api/routes/verify.py tests/test_verify_route_reconstruct.py
git commit -m "feat(verify): persist + reconstruct FixProposal.register

<footer>"
```

---

### Task 6: Dashboard — badges for the new verdicts

**Files:**
- Modify: `src/tvastr/api/templates/app.html`

**Interfaces:**
- Consumes: the three new verdict strings on `verify.result`.

- [ ] **Step 1: Add to `VERDICT_BADGE`**

In `src/tvastr/api/templates/app.html`, in the `VERDICT_BADGE` map (after `verified_via_behavior`), add:

```javascript
  verified_via_warning:      { kind:"green",  label:"verified · warning" },
  verified_via_better_error: { kind:"green",  label:"verified · better error" },
```

and after `unverified_smoke_import_only`, add:

```javascript
  unverified_doc_only:       { kind:"yellow", label:"unverified · doc only" },
```

- [ ] **Step 2: Lock the verify button on the new green verdicts**

Find the `green` button-lock check (currently `lastVerdict === "verified_via_reproducer" || lastVerdict === "verified_via_scoped_tests"`) and extend it:

```javascript
  const green = lastVerdict === "verified_via_reproducer"
    || lastVerdict === "verified_via_scoped_tests"
    || lastVerdict === "verified_via_behavior"
    || lastVerdict === "verified_via_warning"
    || lastVerdict === "verified_via_better_error";
```

(This also closes a pre-existing gap where `verified_via_behavior` didn't lock the button.)

- [ ] **Step 3: Verify the page parses + the badges are present**

Run: `uv run python -c "from pathlib import Path; import tvastr.api.app as a; html=(Path(a.__file__).resolve().parent/'templates'/'app.html').read_text(); assert 'verified_via_warning' in html and 'verified_via_better_error' in html and 'unverified_doc_only' in html; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Suite + lint** — `uv run pytest -q` → green; `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(verify): dashboard badges for warning / better-error / doc-only verdicts

<footer>"
```

---

## Self-Review

**Spec coverage:**
- Register-polymorphic oracle → Task 3 (`_REGISTER_GREEN` map) + Task 2 (register-aware assertion). ✓
- Register-aware synthesis → Task 2. ✓
- `FixProposal.register` default REPAIR / zero regression → Task 1 + verify reads `fix.register`. ✓
- New verdicts + `is_green` → Task 1. ✓
- DOCUMENT short-circuit → Task 3. ✓
- Merged-only base (drop `base.sha`) → Task 4. ✓
- `FixRegister` in `domain` → Task 1. ✓
- Persist/reconstruct register → Task 5. ✓
- Dashboard → Task 6. ✓
- stdlib-only reproducers (no pytest) → Task 2 guidance strings. ✓

**Placeholder scan:** Tasks 3 & 5 reference the existing test helpers (`_FakeSandbox`, `_emit`, `run_path`) the implementer must match by reading the files; all production code blocks are complete. `<footer>` = the two-line co-author/session footer.

**Type consistency:** `FixRegister` (5 members) defined once in `domain`, imported by repro/verifier/route/graph. `register` field/kwarg consistent across `FixProposal`, `_build_prompt`, `synthesize_reproducer`, `verify` (reads `fix.register`). `_REGISTER_GREEN` keys (WARN, BETTER_ERROR) ↔ verdicts (`VERIFIED_VIA_WARNING`, `VERIFIED_VIA_BETTER_ERROR`) ↔ badge keys ↔ `is_green` set — all aligned.
