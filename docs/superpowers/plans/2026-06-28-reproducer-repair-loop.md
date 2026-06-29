# Reproducer Repair Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate verify flakiness from reproducers that hand-roll fakes of external SDK objects (e.g. `FakeOllamaResponse`), by retrying with a repaired reproducer — grounded in the real provisioned dependencies — whenever a verify cycle comes back `REPRO_BROKEN`.

**Architecture:** Extract the baseline→overlay→patch→rerun→triage path into a behavior-preserving `_run_cycle(...) -> _CycleOutcome`, then loop it: on a `REPRO_BROKEN` outcome (the reproducer broke in its own scaffolding, not real code), repair the reproducer from the real error ("import & construct REAL installed objects, never fake external types") and retry, bounded. Provisioning is reordered before synth so cycles run against real deps.

**Tech Stack:** Python 3.11/3.12, Docker/subprocess verify sandbox, pydantic, `uv`, `ruff`, `pytest`.

## Global Constraints

- **Behavior-preserving refactor first (Task 2):** extracting `_run_cycle` must not change any existing verdict; the existing `tests/test_verifier.py` suite is the gate and must pass untouched.
- **Repair trigger = `Verdict.REPRO_BROKEN`** from the full cycle. Real `VERIFIED_*`/`STILL_BROKEN`/`MASKS_SYMPTOM`/`NO_REPRO`/`REGRESSION`/`ENVIRONMENTAL_ERROR` outcomes are returned immediately, no repair.
- **Bounded:** `_MAX_REPRO_REPAIR = 2`. On exhaustion, return the last (`REPRO_BROKEN`) outcome.
- **Gated:** `verify_repro_repair: bool = True` (`TVASTR_VERIFY_REPRO_REPAIR`); `Verifier(..., repro_repair: bool = True)` (mirrors `provision_deps`/`source_overlay`); sealed `false` in tests. Flag off → single cycle, behavior identical to today.
- **Provision-before-synth reorder is unconditional** (synth doesn't depend on the sandbox; same `Reproducer`). Synth moving after `prepare()` means a synth exception now finishes via `self._finish(handle, ENVIRONMENTAL_ERROR, ...)` (discard the handle), not `self._fail(...)`.
- **`_run_cycle` writes `repro.py` from the passed `repro` at its start** (so each attempt runs the current/repaired reproducer); it returns `_CycleOutcome`, never calls `_finish`. `self._finish` (discard + emit `verify.result`) is called ONCE in `verify()` after the loop.
- **Never crash:** `repair_reproducer` returns the original `Reproducer` on any LLM/parse error; all sandbox/LLM exceptions caught.
- Commit footer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`

---

### Task 1: `repair_reproducer` + strengthen the synth prompt

**Files:**
- Modify: `src/tvastr/verification/repro.py`
- Test: `tests/test_repro_repair.py` (new)

**Interfaces:**
- Produces: `repair_reproducer(repro: Reproducer, evidence: dict, pattern: FailurePattern, root_cause: RootCause, router: HybridRouter) -> Reproducer` — re-synthesizes a reproducer given the failing one + the real rerun error; returns the original `Reproducer` on any error. Strengthened `_SYSTEM`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repro_repair.py
from tvastr.domain import FailurePattern, RootCause, Sensitivity
from tvastr.llm.base import LLMResponse
from tvastr.verification.models import Reproducer, ReproducerKind, ReproducerSource
from tvastr.verification.repro import repair_reproducer


class _Router:
    def __init__(self, text, *, boom=False):
        self.text = text
        self.boom = boom
        self.last_prompt = None
        self.last_system = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        if self.boom:
            raise RuntimeError("llm down")
        self.last_prompt = prompt
        self.last_system = system
        return LLMResponse(text=self.text, model="m", target="cloud", mocked=True), None


def _pat():
    return FailurePattern(fingerprint="f", title="t", representative_message="boom",
                          exception_type="AttributeError", sensitivity=Sensitivity.INTERNAL)


def _rc():
    return RootCause(pattern_id="f", summary="s", suspected_files=["base.py"], confidence=0.8)


def _orig():
    return Reproducer(source=ReproducerSource.CLAUDE, code="# tvastr-kind: crash\nold()\n",
                      expected_exception="AttributeError", kind=ReproducerKind.CRASH)


def test_repair_returns_new_reproducer_and_passes_error():
    router = _Router("# tvastr-kind: crash\nfrom ollama import GenerateResponse\nGenerateResponse()\n")
    out = repair_reproducer(_orig(), {"rerun_stderr_tail": "KeyError: 0 in /work/repro.py"},
                            _pat(), _rc(), router)
    assert "GenerateResponse" in out.code
    assert "KeyError: 0" in router.last_prompt          # real error fed back
    assert "real" in (router.last_system or "").lower()  # "use real installed objects" guidance


def test_repair_returns_original_on_error():
    orig = _orig()
    out = repair_reproducer(orig, {"rerun_stderr_tail": "x"}, _pat(), _rc(), _Router("", boom=True))
    assert out is orig
```

- [ ] **Step 2: Run, watch fail** — `uv run pytest tests/test_repro_repair.py -v` → FAIL (`repair_reproducer` missing).

- [ ] **Step 3: Implement** in `src/tvastr/verification/repro.py`.

Add a repair system prompt near `_SYSTEM`:

```python
_REPAIR_SYSTEM = (
    "You are fixing a REPRODUCER that failed in its OWN code, not in the library "
    "under test — so it is untrustworthy. The issue's integration package and its "
    "dependencies ARE installed in the run sandbox. Import and construct the REAL "
    "objects named in the traceback (e.g. `from ollama import GenerateResponse`); "
    "do NOT define fake/stub classes for external library types — fakes don't match "
    "real behavior (e.g. a hand-rolled response that supports `[]` but not `dict()`). "
    "Keep the same reproducer KIND. The FIRST line must be '# tvastr-kind: behavioral' "
    "or '# tvastr-kind: crash'; if behavioral, end with the success marker. Respond "
    "with ONLY Python source — no prose, no fences."
)
```

Add the function (reuse `_strip_fences`/`_parse_kind`):

```python
def repair_reproducer(
    repro: Reproducer,
    evidence: dict,
    pattern: FailurePattern,
    root_cause: RootCause,
    router: HybridRouter,
) -> Reproducer:
    """Re-synthesize a reproducer that failed in its own scaffolding, using the
    real installed deps. Returns the original ``repro`` on any error."""
    error = str(evidence.get("rerun_stderr_tail") or evidence.get("hint") or "")
    expected = repro.expected_exception or pattern.exception_type or "(unknown)"
    prompt = (
        f"The issue: {pattern.title}\nExpected symptom: {expected}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"This reproducer FAILED in its own code (not the library under test):\n"
        f"```\n{repro.code}\n```\n\n"
        f"The real error from running it:\n{error[:1500]}\n\n"
        f"Rewrite it to use the REAL installed objects and reproduce the actual symptom."
    )
    try:
        response, _ = router.run(
            TaskType.FIX_GENERATION, prompt, sensitivity=pattern.sensitivity,
            system=_REPAIR_SYSTEM,
        )
        code = _strip_fences(response.text)
        if not code.strip():
            return repro
        return Reproducer(
            source=ReproducerSource.CLAUDE,
            code=code,
            expected_exception=pattern.exception_type,
            kind=_parse_kind(code),
        )
    except Exception as exc:
        log.warning("verify.repro.repair_failed", error=str(exc))
        return repro
```

Strengthen `_SYSTEM` — append to its existing text:

```python
    " The issue's integration and its dependencies are installed in the run "
    "sandbox: import and construct the REAL classes named in the traceback "
    "(response/SDK objects) rather than defining fake/stub classes for external "
    "library types — fakes (e.g. supporting [] but not dict()) won't match real "
    "behavior and will break under the fix."
```

- [ ] **Step 4: Run, pass** — `uv run pytest tests/test_repro_repair.py tests/test_verifier.py tests/test_repro_register.py -v` → all pass. `uv run ruff check src tests` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/verification/repro.py tests/test_repro_repair.py
git commit -m "feat(verify): repair_reproducer (use real deps) + real-deps synth guidance

<footer>"
```

---

### Task 2: Extract `_run_cycle` (behavior-preserving refactor)

**Files:**
- Modify: `src/tvastr/verification/verifier.py`
- Test: `tests/test_verifier.py` (must pass UNCHANGED — the gate)

**Interfaces:**
- Produces: `_CycleOutcome(verdict: Verdict, oracle: str, evidence: dict)` (small dataclass) and `Verifier._run_cycle(self, handle, repro, fix, pr_number, pr_files) -> _CycleOutcome`.

- [ ] **Step 1: Confirm the current suite is green (baseline)**

Run: `uv run pytest tests/test_verifier.py -q` → all pass. Record the count.

- [ ] **Step 2: Add `_CycleOutcome`**

In `src/tvastr/verification/verifier.py`, add near the top (after imports):

```python
from dataclasses import dataclass


@dataclass
class _CycleOutcome:
    verdict: Verdict
    oracle: str
    evidence: dict
```

- [ ] **Step 3: Extract `_run_cycle`**

Move the block in `verify()` that currently runs from `handle.write_file("repro.py", repro.code)` through the end of the verdict computation (baseline → overlay no_repro → patch → rerun → triage → scoped-tests → final green) into a new method:

```python
    def _run_cycle(self, handle, repro, fix, pr_number, pr_files) -> _CycleOutcome:
        handle.write_file("repro.py", repro.code)
        baseline: RunResult = handle.run(["python", "repro.py"], timeout_s=90)
        ...  # the moved logic, verbatim, EXCEPT every terminal point changes:
```

Transformation rule — replace each terminal `return self._finish(handle, VERDICT, ORACLE, started, EVIDENCE)` with `return _CycleOutcome(VERDICT, ORACLE, EVIDENCE)` (drop `handle`/`started`). The intermediate `self._emit(...)` calls (`verify.baseline`, `verify.overlay`, `verify.patch_applied`, `verify.rerun`, `verify.regression`) stay inside `_run_cycle`. The final green path becomes:

```python
            return _CycleOutcome(green_verdict, green_oracle, {"rerun_exit_code": rerun.exit_code})
```

`_run_cycle` no longer references `started`.

- [ ] **Step 4: Rewrite `verify()`'s tail to call `_run_cycle` once**

After the provision block, replace the moved code with:

```python
            outcome = self._run_cycle(handle, repro, fix, pr_number, pr_files)
            return self._finish(
                handle, outcome.verdict, outcome.oracle, started, outcome.evidence
            )
        finally:
            handle.discard()
```

(The `_finish` + `finally: handle.discard()` stay in `verify()`. Everything before — DOCUMENT short-circuit, synth, prepare, provision — is unchanged in this task.)

- [ ] **Step 5: Run the suite — behavior must be identical**

Run: `uv run pytest tests/test_verifier.py -q` → SAME pass count as Step 1, zero failures.
Run: `uv run pytest -q` → all green. `uv run ruff check src tests` → clean.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/verification/verifier.py
git commit -m "refactor(verify): extract _run_cycle returning _CycleOutcome (no behavior change)

<footer>"
```

---

### Task 3: The repair loop — reorder, wrap, flag, route

**Files:**
- Modify: `src/tvastr/verification/verifier.py` (reorder synth; wrap `_run_cycle`; `repro_repair` kwarg; `_MAX_REPRO_REPAIR`)
- Modify: `src/tvastr/config.py` (flag)
- Modify: `src/tvastr/api/routes/verify.py` (pass the flag)
- Modify: `tests/conftest.py` (seal the flag)
- Test: `tests/test_verifier.py` (repair-loop cases)

**Interfaces:**
- Consumes: `_run_cycle` (Task 2), `repair_reproducer` (Task 1).
- Produces: `Verifier(..., repro_repair: bool = True)`; on a `REPRO_BROKEN` cycle, the reproducer is repaired and the cycle retried (≤ `_MAX_REPRO_REPAIR`); `verify.repro_repair` event.

- [ ] **Step 1: Add the config flag** — in `src/tvastr/config.py`, after `verify_source_overlay`:

```python
    # When true, a verify cycle that comes back REPRO_BROKEN (the reproducer broke
    # in its own scaffolding, e.g. a hand-rolled fake of an SDK object) is retried
    # with a reproducer repaired against the real installed deps. Off in tests.
    verify_repro_repair: bool = True
```

- [ ] **Step 2: Seal in conftest** — in `tests/conftest.py`, after `TVASTR_VERIFY_SOURCE_OVERLAY`:

```python
os.environ["TVASTR_VERIFY_REPRO_REPAIR"] = "false"
```

- [ ] **Step 3: Write the failing repair-loop tests** (reuse `tests/test_verifier.py` helpers — read them first)

```python
# add to tests/test_verifier.py
def test_repro_broken_triggers_repair_then_verifies():
    # cycle 1: baseline reproduces (exit 1), rerun non-zero w/o original → REPRO_BROKEN
    # → repair → cycle 2: baseline reproduces, rerun exit 0 → VERIFIED_VIA_REPRODUCER
    sandbox = _FakeSandbox([
        RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),   # c1 baseline
        RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),                 # c1 rerun → REPRO_BROKEN
        RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),   # c2 baseline
        RunResult(exit_code=0, stdout="ok", stderr=""),                         # c2 rerun → VERIFIED
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=True)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.VERIFIED_VIA_REPRODUCER
    assert any(e.type == "verify.repro_repair" for e in sink.events)


def test_repro_repair_off_returns_repro_broken_once():
    sandbox = _FakeSandbox([
        RunResult(exit_code=1, stdout="", stderr="ModuleNotFoundError: foo"),
        RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=False)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.REPRO_BROKEN
    assert not any(e.type == "verify.repro_repair" for e in sink.events)


def test_repro_repair_budget_exhausted_returns_repro_broken():
    # every cycle REPRO_BROKEN → after _MAX_REPRO_REPAIR repairs, return REPRO_BROKEN
    sandbox = _FakeSandbox([
        RunResult(exit_code=1, stdout="", stderr="x"), RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
        RunResult(exit_code=1, stdout="", stderr="x"), RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
        RunResult(exit_code=1, stdout="", stderr="x"), RunResult(exit_code=1, stdout="", stderr="KeyError: 0"),
    ])
    sink = ListEventSink()
    verifier = Verifier(_ctx(), sandbox, event_sink=sink, repro_repair=True)
    out = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert out.verdict == Verdict.REPRO_BROKEN
    repairs = [e for e in sink.events if e.type == "verify.repro_repair"]
    assert len(repairs) == 2  # _MAX_REPRO_REPAIR
```

> Implementer note: `_ctx()` returns a context whose `MockGitHubClient`/scripted
> router makes `repair_reproducer`'s `router.run` return *some* reproducer text —
> that's fine; the `_FakeSandbox` `RunResult` sequence drives the verdicts, and
> repair only needs to be invoked. Confirm `_FakeSandbox` replays its full
> `responses` list across multiple `run()` calls within one prepared handle (it
> does: `prepare()` copies the list onto the handle). Each `_run_cycle` calls
> `run` twice (baseline, rerun); 3 cycles = 6 responses. The `_fix()` path is
> non-behavioral (crash kind) so a non-zero rerun without the original exception
> → `REPRO_BROKEN`. Match the file's exact helper names.

- [ ] **Step 4: Run, watch fail** — `uv run pytest tests/test_verifier.py -k repro_repair -v` → FAIL.

- [ ] **Step 5: Implement** in `src/tvastr/verification/verifier.py`:

Add the import + constant near the top:

```python
from tvastr.verification.repro import repair_reproducer, synthesize_reproducer

_MAX_REPRO_REPAIR = 2
```

Add the ctor kwarg (after `source_overlay`):

```python
        repro_repair: bool = True,
    ) -> None:
        ...
        self.repro_repair = repro_repair
```

Reorder + wrap in `verify()`. After the provision block (synth MOVED to here, after provision; remove the old pre-`prepare` synth):

```python
        handle = self.sandbox.prepare()
        try:
            <provision block unchanged>

            # Synthesize AFTER provisioning so the reproducer (and any repair) runs
            # against the real installed deps.
            try:
                repro = synthesize_reproducer(
                    pattern, root_cause, sample_events, issue_body, self.ctx.router,
                    code_context=_code_context, register=fix.register,
                )
            except Exception as exc:
                return self._finish(
                    handle, Verdict.ENVIRONMENTAL_ERROR, "none", started,
                    {"stage": "repro_synth", "error": str(exc)},
                )
            self._emit("verify.repro_synth", "verify",
                       {"source": repro.source.value, "code": repro.code,
                        "expected_exception": repro.expected_exception,
                        "register": fix.register.value})

            for attempt in range(_MAX_REPRO_REPAIR + 1):
                outcome = self._run_cycle(handle, repro, fix, pr_number, pr_files)
                if (outcome.verdict != Verdict.REPRO_BROKEN
                        or not self.repro_repair
                        or attempt == _MAX_REPRO_REPAIR):
                    return self._finish(
                        handle, outcome.verdict, outcome.oracle, started, outcome.evidence
                    )
                self._emit("verify.repro_repair", "verify",
                           {"attempt": attempt + 1,
                            "error_tail": outcome.evidence.get("rerun_stderr_tail", "")})
                repro = repair_reproducer(repro, outcome.evidence, pattern, root_cause,
                                          self.ctx.router)
        finally:
            handle.discard()
```

Delete the now-moved original synth block (the one before `handle = self.sandbox.prepare()`) and its old `self._emit("verify.repro_synth", ...)`. Keep the DOCUMENT short-circuit and `_code_context` definition before `prepare()` (they don't need the sandbox; `_code_context` is referenced by the moved synth).

- [ ] **Step 6: Pass the flag at the route** — in `src/tvastr/api/routes/verify.py`, the `Verifier(...)` construction, add:

```python
        repro_repair=settings.verify_repro_repair,
```

- [ ] **Step 7: Run repair-loop tests + full suite + lint**

Run: `uv run pytest tests/test_verifier.py -v` → all pass (new + existing).
Run: `uv run pytest -q` → all green (report count).
Run: `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
git add src/tvastr/verification/verifier.py src/tvastr/config.py src/tvastr/api/routes/verify.py tests/conftest.py tests/test_verifier.py
git commit -m "feat(verify): repair reproducer + retry on REPRO_BROKEN (real-deps grounding)

<footer>"
```

---

### Task 4: Dashboard — render `verify.repro_repair`

**Files:**
- Modify: `src/tvastr/api/templates/app.html`

- [ ] **Step 1: Add a summary label** — in the `summarize` switch, near the other `verify.*` cases, add:

```javascript
    case "verify.repro_repair": return `reproducer repair #${p.attempt} (was REPRO_BROKEN) · ${(p.error_tail||"").slice(0,60)}`;
```

- [ ] **Step 2: Verify the page parses**

Run: `uv run python -c "from pathlib import Path; import tvastr.api.app as a; html=(Path(a.__file__).resolve().parent/'templates'/'app.html').read_text(); assert 'verify.repro_repair' in html; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Suite + lint** — `uv run pytest -q` → green; `uv run ruff check src tests` → clean.

- [ ] **Step 4: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(verify): surface verify.repro_repair in the dashboard timeline

<footer>"
```

---

## Self-Review

**Spec coverage:**
- `repair_reproducer` + real-deps synth guidance → Task 1. ✓
- `_run_cycle` extraction (behavior-preserving) → Task 2. ✓
- Repair loop, trigger=`REPRO_BROKEN`, bounded, gated, provision-before-synth reorder → Task 3. ✓
- Config flag + conftest seal + route → Task 3. ✓
- Synth-after-prepare → finish via `_finish(ENVIRONMENTAL_ERROR)` → Task 3 Step 5. ✓
- Never-crash (repair returns original; flag off = single cycle) → Tasks 1 & 3. ✓
- UI → Task 4. ✓

**Placeholder scan:** Task 2 Step 3 is a mechanical move+transform of existing code (the implementer reads the file; the transform rule is exact); Task 3 Step 5 shows the full new control flow. Test scaffolding in Tasks 1 & 3 is concrete; the Task 3 note points to `test_verifier.py` helpers. `<footer>` = the two-line co-author/session footer.

**Type consistency:** `_CycleOutcome(verdict, oracle, evidence)` produced by `_run_cycle`, consumed by `verify()`. `repair_reproducer(repro, evidence, pattern, root_cause, router) -> Reproducer` consistent (Task 1 def ↔ Task 3 call). `repro_repair` consistent (config `verify_repro_repair` → ctor `repro_repair` → route). `_MAX_REPRO_REPAIR` used in the loop bound + asserted in the budget test.
