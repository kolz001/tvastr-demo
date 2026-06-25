# Behavioral Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the verify-fix reproducer from crash-trigger to behavioral (setup → exercise → assert expected result → success sentinel) so symptom-masking fixes are caught as `MASKS_SYMPTOM` instead of passing as green.

**Architecture:** A behavioral reproducer ends with `print("TVASTR_BEHAVIOR_OK")`, reached only if its assertions pass. The verifier branches on `Reproducer.kind`: behavioral runs use the sentinel to discriminate post-patch into `VERIFIED_VIA_BEHAVIOR` / `MASKS_SYMPTOM` / `STILL_BROKEN` / `REPRO_BROKEN`; crash reproducers keep today's exact logic. Synthesis prefers a behavioral reproducer and falls back to crash-trigger when expected behavior can't be inferred.

**Tech Stack:** Python 3.12; the existing verification loop (`verifier.py` / `repro.py` / `models.py`), its `_FakeSandbox`+stub-router test harness, and the single-file UI.

## Global Constraints

- Success sentinel constant: `BEHAVIOR_OK_MARKER = "TVASTR_BEHAVIOR_OK"`. Green for a behavioral repro requires **exit 0 AND the marker in stdout**.
- `Reproducer.kind: ReproducerKind` (`BEHAVIORAL` | `CRASH`), default `CRASH`. The crash path (kind=CRASH) must behave EXACTLY as today — existing verifier tests stay green unchanged.
- Synthesis: Claude tags the first line `# tvastr-kind: behavioral` or `# tvastr-kind: crash`; parse it, default `CRASH` when absent/unparseable. The no-Claude `ISSUE_BODY` fast-path stays `CRASH`.
- Post-patch behavioral triage (after `still_broken`/timeout checks): marker+exit0 → `VERIFIED_VIA_BEHAVIOR` (oracle="behavior"); `AssertionError` in stderr → `MASKS_SYMPTOM` (oracle="behavior", NOT green); else → `REPRO_BROKEN` (oracle="none").
- Behavioral baseline "reproduced" = marker **absent** pre-patch; if present → `NO_REPRO`.
- `is_green` adds `VERIFIED_VIA_BEHAVIOR` only — `MASKS_SYMPTOM` is NOT green.
- No changes to the router, agent graph, sandbox, or scoped-tests regression step (it still runs only after a green reproducer — now behavioral or crash green).
- MANDATORY before each commit: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/verification/models.py` (modify) | two verdicts, `is_green`, `ReproducerKind`, `Reproducer.kind`, `BEHAVIOR_OK_MARKER` |
| `src/tvastr/verification/repro.py` (modify) | behavioral-preferred prompt + `# tvastr-kind:` parsing → `Reproducer.kind` |
| `src/tvastr/verification/verifier.py` (modify) | behavioral baseline + post-patch triage, keyed on `repro.kind` |
| `src/tvastr/api/templates/app.html` (modify) | two `VERDICT_BADGE` entries |
| `tests/test_verifier.py` / `tests/test_repro.py` | behavioral triage + kind-parsing + is_green tests |

---

## Task 1: Models — verdicts, kind, sentinel, is_green

**Files:**
- Modify: `src/tvastr/verification/models.py`
- Test: `tests/test_verification_models.py` (create)

**Interfaces:**
- Produces: `Verdict.VERIFIED_VIA_BEHAVIOR = "verified_via_behavior"`, `Verdict.MASKS_SYMPTOM = "masks_symptom"`; `ReproducerKind(StrEnum)` with `BEHAVIORAL = "behavioral"` / `CRASH = "crash"`; `Reproducer.kind: ReproducerKind = ReproducerKind.CRASH`; module constant `BEHAVIOR_OK_MARKER = "TVASTR_BEHAVIOR_OK"`; `VerificationResult.is_green` includes `VERIFIED_VIA_BEHAVIOR`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verification_models.py`:

```python
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    ReproducerSource,
    Verdict,
    VerificationResult,
)


def test_new_verdicts_exist():
    assert Verdict.VERIFIED_VIA_BEHAVIOR.value == "verified_via_behavior"
    assert Verdict.MASKS_SYMPTOM.value == "masks_symptom"


def test_behavior_marker_constant():
    assert BEHAVIOR_OK_MARKER == "TVASTR_BEHAVIOR_OK"


def test_reproducer_kind_defaults_to_crash():
    r = Reproducer(source=ReproducerSource.CLAUDE, code="x")
    assert r.kind == ReproducerKind.CRASH


def test_reproducer_kind_can_be_behavioral():
    r = Reproducer(source=ReproducerSource.CLAUDE, code="x", kind=ReproducerKind.BEHAVIORAL)
    assert r.kind == ReproducerKind.BEHAVIORAL


def test_is_green_includes_behavior_excludes_masks():
    def _r(v):
        return VerificationResult(verdict=v, oracle="behavior", elapsed_s=0.0)
    assert _r(Verdict.VERIFIED_VIA_BEHAVIOR).is_green is True
    assert _r(Verdict.MASKS_SYMPTOM).is_green is False
    assert _r(Verdict.VERIFIED_VIA_REPRODUCER).is_green is True  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verification_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'BEHAVIOR_OK_MARKER'` / `ReproducerKind`.

- [ ] **Step 3: Implement the model changes**

In `src/tvastr/verification/models.py`:

Add the two verdicts to `Verdict` (after `VERIFIED_VIA_SCOPED_TESTS`):

```python
    VERIFIED_VIA_REPRODUCER = "verified_via_reproducer"
    VERIFIED_VIA_SCOPED_TESTS = "verified_via_scoped_tests"
    VERIFIED_VIA_BEHAVIOR = "verified_via_behavior"
    MASKS_SYMPTOM = "masks_symptom"
    UNVERIFIED_SMOKE_IMPORT_ONLY = "unverified_smoke_import_only"
```

Add the sentinel constant and `ReproducerKind` (after the `Verdict` class, before `ReproducerSource`):

```python
BEHAVIOR_OK_MARKER = "TVASTR_BEHAVIOR_OK"


class ReproducerKind(StrEnum):
    """Whether the reproducer asserts correct behavior or just re-triggers the crash."""

    BEHAVIORAL = "behavioral"
    CRASH = "crash"
```

Add `kind` to `Reproducer`:

```python
@dataclass(frozen=True)
class Reproducer:
    source: ReproducerSource
    code: str
    expected_exception: str | None = None  # exception class we're trying to re-trigger
    kind: ReproducerKind = ReproducerKind.CRASH
```

Update `is_green` to include the behavioral green:

```python
    @property
    def is_green(self) -> bool:
        return self.verdict in {
            Verdict.VERIFIED_VIA_REPRODUCER,
            Verdict.VERIFIED_VIA_SCOPED_TESTS,
            Verdict.VERIFIED_VIA_BEHAVIOR,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_verification_models.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Regression + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean (existing verifier tests unaffected — `kind` defaults to `CRASH`).

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/verification/models.py tests/test_verification_models.py
git commit -m "feat(verify): add behavioral verdicts, ReproducerKind, sentinel constant"
```

---

## Task 2: Synthesis — behavioral-preferred reproducer + kind parsing

**Files:**
- Modify: `src/tvastr/verification/repro.py`
- Test: `tests/test_repro.py` (create)

**Interfaces:**
- Consumes: `ReproducerKind`, `BEHAVIOR_OK_MARKER` (Task 1).
- Produces: `synthesize_reproducer(...)` returns a `Reproducer` whose `kind` is parsed from the model's first-line `# tvastr-kind:` tag (default `CRASH`); the `ISSUE_BODY` fast-path returns `kind=CRASH`. New helper `_parse_kind(code: str) -> ReproducerKind`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repro.py`:

```python
from dataclasses import dataclass

from tvastr.config import Settings
from tvastr.domain import FailurePattern, LogEvent, RootCause, Sensitivity, Severity
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router
from tvastr.verification.models import BEHAVIOR_OK_MARKER, ReproducerKind, ReproducerSource
from tvastr.verification.repro import _build_prompt, _parse_kind, synthesize_reproducer


@dataclass
class _ScriptedLLM:
    response_text: str
    model: str = "claude-opus-4-7"
    target: str = "cloud"

    def complete(self, prompt, *, system=None):
        return LLMResponse(text=self.response_text, model=self.model, target=self.target, mocked=True)


def _ctx_router(text):
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = _ScriptedLLM(text)
    return router


def _pattern():
    return FailurePattern(
        fingerprint="abc", title="KeyError in VectorMemory",
        representative_message="KeyError: 'sub_dicts'", exception_type="KeyError",
        count=3, sensitivity=Sensitivity.INTERNAL,
    )


def _root_cause():
    return RootCause(pattern_id="abc", summary="missing key", suspected_files=["vm.py"], confidence=0.8)


def test_parse_kind_behavioral():
    assert _parse_kind("# tvastr-kind: behavioral\nimport x\n") == ReproducerKind.BEHAVIORAL


def test_parse_kind_crash():
    assert _parse_kind("# tvastr-kind: crash\nimport x\n") == ReproducerKind.CRASH


def test_parse_kind_defaults_crash_when_absent():
    assert _parse_kind("import x\nprint(1)\n") == ReproducerKind.CRASH


def test_synthesize_sets_behavioral_kind():
    code = f"# tvastr-kind: behavioral\nassert True\nprint('{BEHAVIOR_OK_MARKER}')\n"
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, _ctx_router(code))
    assert repro.kind == ReproducerKind.BEHAVIORAL
    assert repro.source == ReproducerSource.CLAUDE


def test_synthesize_defaults_crash_kind():
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, _ctx_router("raise KeyError\n"))
    assert repro.kind == ReproducerKind.CRASH


def test_issue_body_fastpath_is_crash_kind():
    body = "Repro:\n```python\nimport foo\nfoo.bar()\n```\n"
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], body, _ctx_router("unused"))
    assert repro.source == ReproducerSource.ISSUE_BODY
    assert repro.kind == ReproducerKind.CRASH


def test_prompt_mentions_behavioral_marker_and_fallback():
    prompt = _build_prompt(_pattern(), _root_cause(), [])
    assert BEHAVIOR_OK_MARKER in prompt
    assert "tvastr-kind" in prompt
```

> NOTE: `test_issue_body_fastpath_is_crash_kind` relies on `extract_from_body` recognizing a fenced python block. If the existing extractor needs a specific shape, the implementer should match the form used by existing `tests/test_repro*`/verifier tests; the assertion that matters is `kind == CRASH` on the body path.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repro.py -v`
Expected: FAIL — `ImportError: cannot import name '_parse_kind'`.

- [ ] **Step 3: Rewrite the synthesis prompt to prefer behavioral**

In `src/tvastr/verification/repro.py`, replace `_SYSTEM`:

```python
_SYSTEM = (
    "You are a senior Python engineer. Produce a MINIMAL reproducer (5–20 lines) "
    "for the failure below, runnable on a clean install of the target project.\n\n"
    "PREFER a BEHAVIORAL reproducer: set up the scenario, exercise the buggy "
    "operation, and ASSERT the expected CORRECT result (e.g. data round-trips, "
    "the returned value equals what was stored). End the script with exactly:\n"
    f"    print(\"{BEHAVIOR_OK_MARKER}\")\n"
    "so the marker prints ONLY if every assertion passed. A fix that merely "
    "suppresses the error without restoring behavior must fail your assertion.\n\n"
    "If you CANNOT determine the expected correct behavior from the issue and "
    "code, FALL BACK to a crash reproducer that simply re-triggers the original "
    "exception (no assertion, no marker).\n\n"
    "The FIRST line of your response MUST be one of:\n"
    "    # tvastr-kind: behavioral\n"
    "    # tvastr-kind: crash\n"
    "If actual source of the suspected files is shown, use ONLY APIs that appear "
    "in it; do not invent constructor parameters. Respond with ONLY Python source "
    "— no prose, no markdown fences."
)
```

Add the import of the new names at the top (find the existing `from tvastr.verification.models import ...` line and extend it):

```python
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    ReproducerSource,
)
```

(Keep whatever else that import line already pulls in; just ensure `BEHAVIOR_OK_MARKER` and `ReproducerKind` are included.)

- [ ] **Step 4: Add `_parse_kind` and set `kind` on the Claude path**

Add the helper (after `_strip_fences`):

```python
def _parse_kind(code: str) -> ReproducerKind:
    """Read the leading `# tvastr-kind:` tag; default CRASH when absent/unknown."""
    first = code.lstrip().splitlines()[0] if code.strip() else ""
    if "tvastr-kind:" in first and "behavioral" in first:
        return ReproducerKind.BEHAVIORAL
    return ReproducerKind.CRASH
```

In `synthesize_reproducer`, the `ISSUE_BODY` return is unchanged (kind defaults to `CRASH`). Change the final Claude-path return to parse and set the kind:

```python
    code = _strip_fences(response.text)
    kind = _parse_kind(code)
    log.info("verify.repro.synthesized", lines=code.count("\n") + 1, model=response.model, kind=kind.value)
    return Reproducer(
        source=ReproducerSource.CLAUDE,
        code=code,
        expected_exception=pattern.exception_type,
        kind=kind,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_repro.py -v`
Expected: PASS (7 tests). If `test_issue_body_fastpath_is_crash_kind` fails on extraction shape, adjust the body string to a form `extract_from_body` accepts (the `kind == CRASH` assertion is the point).

- [ ] **Step 6: Regression + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean.

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/verification/repro.py tests/test_repro.py
git commit -m "feat(verify): behavioral-preferred reproducer synthesis + kind parsing"
```

---

## Task 3: Verifier behavioral triage + UI badges

**Files:**
- Modify: `src/tvastr/verification/verifier.py`
- Modify: `src/tvastr/api/templates/app.html`
- Test: `tests/test_verifier.py`

**Interfaces:**
- Consumes: `Reproducer.kind`, `ReproducerKind`, `BEHAVIOR_OK_MARKER`, the two new verdicts (Tasks 1–2).
- Produces: behavioral post-patch triage in `Verifier.verify`; two `VERDICT_BADGE` entries.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verifier.py` (helpers `_FakeSandbox`, `_ctx`, `_pattern`, `_root_cause`, `_fix`, `_event` already exist). These drive a **behavioral** reproducer by scripting the synth response with the kind tag + marker, and scripting baseline/rerun `RunResult`s:

```python
from tvastr.verification.models import BEHAVIOR_OK_MARKER


_BEHAVIORAL_REPRO = (
    f"# tvastr-kind: behavioral\nassert get() == put_value\nprint('{BEHAVIOR_OK_MARKER}')\n"
)


def test_behavioral_verified_when_marker_present_post_patch() -> None:
    sandbox = _FakeSandbox(
        [
            # baseline: bug reproduces (KeyError, no marker)
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            # rerun after real fix: marker printed, exit 0
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.VERIFIED_VIA_BEHAVIOR
    assert result.oracle == "behavior"
    assert result.is_green


def test_behavioral_masks_symptom_when_assertion_fails_post_patch() -> None:
    # Regression for #21896: masking fix stops the crash but the assertion fails.
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="Traceback...\nAssertionError"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.MASKS_SYMPTOM
    assert not result.is_green
    assert result.oracle == "behavior"


def test_behavioral_still_broken_when_original_exception_remains() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.STILL_BROKEN


def test_behavioral_repro_broken_on_other_exception() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=1, stdout="", stderr="KeyError: 'sub_dicts'"),
            RunResult(exit_code=1, stdout="", stderr="TypeError: unexpected kwarg"),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.REPRO_BROKEN


def test_behavioral_no_repro_when_baseline_prints_marker() -> None:
    sandbox = _FakeSandbox(
        [
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),  # baseline already passes
            RunResult(exit_code=0, stdout=f"{BEHAVIOR_OK_MARKER}\n", stderr=""),
        ]
    )
    verifier = Verifier(_ctx(_BEHAVIORAL_REPRO), sandbox)
    result = verifier.verify(_pattern(), _root_cause(), _fix(), [_event()], issue_body=None)
    assert result.verdict == Verdict.NO_REPRO
```

> NOTE: `_pattern()` has `exception_type="ModuleNotFoundError"`; the behavioral tests script `KeyError` in stderr. `_original_exception_seen` checks the pattern's exception in stderr, so for the STILL_BROKEN test the stderr must contain the pattern's `exception_type`. Change those tests' stderr strings to the pattern's exception (`ModuleNotFoundError`) OR add a `_kerr_pattern()` helper whose `exception_type="KeyError"`. Use a local `_kerr_pattern()` returning a `FailurePattern(..., exception_type="KeyError")` and pass it to these five tests so the original-exception detection is exercised correctly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verifier.py -k behavioral -v`
Expected: FAIL — behavioral repros currently fall through the crash path; marker/AssertionError are not interpreted, so verdicts are wrong (e.g. `VERIFIED_VIA_REPRODUCER` instead of `VERIFIED_VIA_BEHAVIOR`, masking case green).

- [ ] **Step 3: Add the behavioral baseline check**

In `src/tvastr/verification/verifier.py`, extend the imports:

```python
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    RunResult,
)
```

(Merge with the existing `from tvastr.verification.models import ...` line — keep current names, add `BEHAVIOR_OK_MARKER` and `ReproducerKind`.)

Find the baseline NO_REPRO guard:

```python
            if not saw_original and baseline.succeeded:
```

Replace it with a kind-aware "did the bug reproduce?" check:

```python
            is_behavioral = repro.kind == ReproducerKind.BEHAVIORAL
            if is_behavioral:
                baseline_reproduced = BEHAVIOR_OK_MARKER not in baseline.stdout
            else:
                baseline_reproduced = saw_original or not baseline.succeeded
            if not baseline_reproduced:
```

(The body of the `if` — the `NO_REPRO` `return self._finish(...)` — stays exactly as is.)

- [ ] **Step 4: Add the behavioral post-patch triage**

In the same method, after the `if still_broken: return STILL_BROKEN` block and the `if rerun.timed_out: return ENVIRONMENTAL_ERROR` block, replace the crash-only `if rerun.exit_code != 0: REPRO_BROKEN` block plus the final `VERIFIED_VIA_REPRODUCER` return with a kind-aware structure. Concretely, replace this existing region:

```python
            if rerun.exit_code != 0:
                return self._finish(
                    handle,
                    Verdict.REPRO_BROKEN,
                    "none",
                    started,
                    {
                        "rerun_exit_code": rerun.exit_code,
                        "rerun_stderr_tail": _tail(rerun.stderr),
                        "hint": (
                            "reproducer raised a different exception than the "
                            "issue's original — Claude likely referenced an "
                            "API that doesn't exist; retry to resynthesise."
                        ),
                    },
                )
```

with the behavioral/crash split that decides the green verdict (and short-circuits MASKS_SYMPTOM / REPRO_BROKEN):

```python
            if is_behavioral:
                if rerun.exit_code == 0 and BEHAVIOR_OK_MARKER in rerun.stdout:
                    green_verdict = Verdict.VERIFIED_VIA_BEHAVIOR
                    green_oracle = "behavior"
                elif "AssertionError" in rerun.stderr:
                    # Crash suppressed, but the behavioral assertion failed: the
                    # fix masks the symptom without restoring behavior.
                    return self._finish(
                        handle,
                        Verdict.MASKS_SYMPTOM,
                        "behavior",
                        started,
                        {
                            "rerun_exit_code": rerun.exit_code,
                            "rerun_stderr_tail": _tail(rerun.stderr),
                            "hint": (
                                "the fix stopped the exception but the behavioral "
                                "assertion failed — the symptom is masked, not fixed."
                            ),
                        },
                    )
                else:
                    return self._finish(
                        handle,
                        Verdict.REPRO_BROKEN,
                        "none",
                        started,
                        {
                            "rerun_exit_code": rerun.exit_code,
                            "rerun_stderr_tail": _tail(rerun.stderr),
                            "hint": "behavioral reproducer neither asserted-OK nor failed cleanly.",
                        },
                    )
            else:
                if rerun.exit_code != 0:
                    return self._finish(
                        handle,
                        Verdict.REPRO_BROKEN,
                        "none",
                        started,
                        {
                            "rerun_exit_code": rerun.exit_code,
                            "rerun_stderr_tail": _tail(rerun.stderr),
                            "hint": (
                                "reproducer raised a different exception than the "
                                "issue's original — Claude likely referenced an "
                                "API that doesn't exist; retry to resynthesise."
                            ),
                        },
                    )
                green_verdict = Verdict.VERIFIED_VIA_REPRODUCER
                green_oracle = "reproducer"
```

Then change the final green return (currently hard-coded `Verdict.VERIFIED_VIA_REPRODUCER` / `"reproducer"`) to use the chosen verdict. Replace:

```python
            return self._finish(
                handle,
                Verdict.VERIFIED_VIA_REPRODUCER,
                "reproducer",
                started,
```

with:

```python
            return self._finish(
                handle,
                green_verdict,
                green_oracle,
                started,
```

(The scoped-tests regression block in between is unchanged — it still runs before this final return and can still short-circuit to `REGRESSION`.)

- [ ] **Step 5: Run the verifier tests to verify they pass**

Run: `uv run pytest tests/test_verifier.py -v`
Expected: PASS — the five new behavioral tests plus all existing crash-path tests (crash repros use `green_verdict = VERIFIED_VIA_REPRODUCER`, identical to before).

- [ ] **Step 6: Add the UI verdict badges**

In `src/tvastr/api/templates/app.html`, find `const VERDICT_BADGE = {` and add two entries (after `verified_via_scoped_tests`):

```javascript
  verified_via_behavior:     { kind:"green",  label:"verified · behavior" },
  masks_symptom:             { kind:"red",    label:"masks symptom" },
```

- [ ] **Step 7: Full suite + lint + app boot**

Run: `uv run pytest -q && uv run ruff check src tests && uv run python -c "from tvastr.api.app import create_app; create_app(); print('OK')"`
Expected: all green, lint clean, `OK`.

- [ ] **Step 8: Commit**

```bash
git add src/tvastr/verification/verifier.py src/tvastr/api/templates/app.html tests/test_verifier.py
git commit -m "feat(verify): behavioral post-patch triage (masks_symptom / verified_via_behavior) + UI badges"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With live mode + Anthropic key, run #21896 through `/app`, then click Verify (or trigger verify). Confirm the synthesized reproducer is behavioral (asserts messages round-trip, ends with the marker) and the verdict on the agent's masking fix is **`masks symptom`** (red), not a green. A behavior-restoring fix would read `verified · behavior`.

## Self-Review (completed by author)

- **Spec coverage:** sentinel discriminator + green=exit0&marker (Task 3 Step 4); `ReproducerKind`/`Reproducer.kind`/two verdicts/`is_green`/`BEHAVIOR_OK_MARKER` (Task 1); behavioral-preferred prompt + `# tvastr-kind:` parse + crash fallback + ISSUE_BODY=CRASH (Task 2); behavioral baseline "reproduced = marker absent" (Task 3 Step 3); post-patch table → BEHAVIOR/MASKS/STILL/REPRO with oracle values (Task 3 Step 4); crash path unchanged (Task 3 else-branch identical to today + existing tests); UI badges (Task 3 Step 6); scoped-tests unchanged (left in place before the final return); #21896 regression test + live metric.
- **Placeholder scan:** none — every code/test step has complete code; the two NOTEs give concrete alternatives, not TODOs.
- **Type consistency:** `ReproducerKind.BEHAVIORAL/CRASH`, `Reproducer(..., kind=...)`, `BEHAVIOR_OK_MARKER`, `green_verdict`/`green_oracle`, `Verdict.VERIFIED_VIA_BEHAVIOR`/`MASKS_SYMPTOM` used identically across tasks; verdict string values match the UI badge keys (`verified_via_behavior`, `masks_symptom`).
