# Reproducer Oracle Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the behavioral reproducer assert *intended* behavior (so a symptom-masking fix fails it) via a hardened synthesis prompt plus a one-pass self-critique gate.

**Architecture:** Harden `_SYSTEM` in `repro.py` against asserting the degraded state; add `_critique_reproducer` (a `TaskType.REPRO_CRITIQUE` call) that `synthesize_reproducer` runs on Claude **behavioral** reproducers to rewrite weak oracles. Contained to `repro.py` + one router task type.

**Tech Stack:** Python 3.12, the project `HybridRouter` (mock/stub-router test seam), the verification reproducer module.

## Global Constraints

- `TaskType.REPRO_CRITIQUE = "repro_critique"` — a cloud task (NOT in `_LOCAL_TASKS`), so the router redacts it.
- The critique runs **iff** the synthesized reproducer's kind is `BEHAVIORAL` (crash repros, the `ISSUE_BODY` fast-path, and mock mode skip it — mock prose parses as `CRASH`).
- Exactly **one** critique pass — no loop.
- Graceful: a critique call that raises → return the original code. Guard: if the rewrite is empty, OR it still claims `behavioral` but lacks `BEHAVIOR_OK_MARKER` → keep the original. An explicit downgrade to `# tvastr-kind: crash` is honored.
- Prompt hardening (`_SYSTEM`): forbid mocking/asserting the degraded state; require real input + end-to-end + round-trip/expected assertion; a suppress-only fix must fail.
- No changes to the verifier, sandbox, or agent graph. Existing `test_repro.py` + verifier tests stay green.
- MANDATORY before each commit: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/llm/router.py` (modify) | `TaskType.REPRO_CRITIQUE` |
| `src/tvastr/verification/repro.py` (modify) | hardened `_SYSTEM`; `_CRITIQUE_SYSTEM`; `_critique_reproducer`; critique wiring in `synthesize_reproducer` |
| `tests/test_repro.py` (modify) | task-type, prompt-content, critique unit + end-to-end + gating + graceful tests |

---

## Task 1: Router task type + hardened synthesis prompt

**Files:**
- Modify: `src/tvastr/llm/router.py`, `src/tvastr/verification/repro.py`
- Test: `tests/test_repro.py`

**Interfaces:**
- Produces: `TaskType.REPRO_CRITIQUE = "repro_critique"` (cloud); hardened `_SYSTEM` string in `repro.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repro.py`:

```python
def test_repro_critique_is_a_cloud_task():
    from tvastr.llm.router import _LOCAL_TASKS, TaskType
    assert TaskType.REPRO_CRITIQUE.value == "repro_critique"
    assert TaskType.REPRO_CRITIQUE not in _LOCAL_TASKS


def test_system_prompt_forbids_asserting_degraded_state():
    from tvastr.verification.repro import _SYSTEM
    s = _SYSTEM
    assert "round-trip" in s
    assert "FORBIDDEN" in s
    assert "degraded" in s
    assert "SUPPRESSES" in s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repro.py -k "repro_critique_is_a_cloud or forbids_asserting" -v`
Expected: FAIL — `AttributeError: REPRO_CRITIQUE`; `_SYSTEM` lacks the new phrases.

- [ ] **Step 3: Add the task type**

In `src/tvastr/llm/router.py`, add to `TaskType` (after `DOC_GROUNDING`):

```python
    DOC_GROUNDING = "doc_grounding"
    REPRO_CRITIQUE = "repro_critique"
```

- [ ] **Step 4: Harden `_SYSTEM`**

In `src/tvastr/verification/repro.py`, replace the entire `_SYSTEM = (...)` assignment with:

```python
_SYSTEM = (
    "You are a senior Python engineer. Produce a MINIMAL reproducer (5-20 lines) "
    "for the failure below, runnable on a clean install of the target project.\n\n"
    "PREFER a BEHAVIORAL reproducer: set up REAL, valid input, exercise the FULL "
    "operation end-to-end, and ASSERT the user-visible output reflects that input "
    "(e.g. data round-trips: what you get back equals what you put in) or a stated "
    "expected value. End the script with exactly:\n"
    f"    print(\"{BEHAVIOR_OK_MARKER}\")\n"
    "so the marker prints ONLY if every assertion passed.\n\n"
    "CRITICAL - make the oracle strong:\n"
    "- Do NOT mock or hand-construct the already-broken/degraded intermediate "
    "state and then assert that degraded output. Drive the real operation with "
    "valid input instead.\n"
    "- Asserting an empty/None/degenerate result is FORBIDDEN unless empty is "
    "genuinely the correct outcome for valid input.\n"
    "- A fix that merely SUPPRESSES the error (returns empty/default without "
    "restoring behavior) MUST FAIL your assertion.\n\n"
    "If you CANNOT determine the expected correct behavior from the issue and "
    "code, FALL BACK to a crash reproducer that simply re-triggers the original "
    "exception (no assertion, no marker).\n\n"
    "The FIRST line of your response MUST be one of:\n"
    "    # tvastr-kind: behavioral\n"
    "    # tvastr-kind: crash\n"
    "If actual source of the suspected files is shown, use ONLY APIs that appear "
    "in it; do not invent constructor parameters. Respond with ONLY Python source "
    "- no prose, no markdown fences."
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_repro.py -k "repro_critique_is_a_cloud or forbids_asserting" -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Regression + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean (existing repro/verifier tests unaffected — the prompt text changed but kind-parsing + synthesis flow are unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/llm/router.py src/tvastr/verification/repro.py tests/test_repro.py
git commit -m "feat(verify): REPRO_CRITIQUE task type + harden reproducer prompt against degraded-state oracles"
```

---

## Task 2: Self-critique gate

**Files:**
- Modify: `src/tvastr/verification/repro.py`
- Test: `tests/test_repro.py`

**Interfaces:**
- Consumes: `TaskType.REPRO_CRITIQUE` (Task 1); `BEHAVIOR_OK_MARKER`, `ReproducerKind`, `_parse_kind`, `_strip_fences`, `_truncate` (existing in repro.py).
- Produces: `_critique_reproducer(code: str, pattern, root_cause, code_context: str, router) -> str`; `synthesize_reproducer` runs it on behavioral Claude repros.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repro.py` (the file already imports `Settings`, `FailurePattern`, `RootCause`, `Sensitivity`, `LLMResponse`, `build_router`, and defines `_pattern`/`_root_cause`/`_ScriptedLLM` from Task-2-of-behavioral work; add the `RoutingDecision` import + `_SeqRouter` helper shown here):

```python
from tvastr.domain import RoutingDecision
from tvastr.verification.models import BEHAVIOR_OK_MARKER, ReproducerKind
from tvastr.verification.repro import _critique_reproducer, synthesize_reproducer

_WEAK = f"# tvastr-kind: behavioral\nnode = mk_empty()\nassert node_get() == []\nprint('{BEHAVIOR_OK_MARKER}')\n"
_STRONG = f"# tvastr-kind: behavioral\nput(msgs)\nassert get() == msgs\nprint('{BEHAVIOR_OK_MARKER}')\n"
_CRASH = "# tvastr-kind: crash\nraise KeyError('sub_dicts')\n"


class _SeqRouter:
    """Router stub returning a scripted sequence of responses, recording tasks."""

    def __init__(self, responses, raise_on=None):
        self._responses = list(responses)
        self.tasks = []
        self._raise_on = raise_on  # a TaskType to raise on, or None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.tasks.append(task)
        if self._raise_on is not None and task == self._raise_on:
            raise RuntimeError("critique boom")
        text = self._responses.pop(0)
        decision = RoutingDecision(
            task=task.value, target="cloud", model="seq", sensitivity=sensitivity, reason="seq"
        )
        return LLMResponse(text=text, model="seq", target="cloud", mocked=True), decision


def test_critique_returns_strengthened(_=None):
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_STRONG])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _STRONG
    assert r.tasks == [TaskType.REPRO_CRITIQUE]


def test_critique_graceful_on_error():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([], raise_on=TaskType.REPRO_CRITIQUE)
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK  # original kept on failure


def test_critique_keeps_original_when_rewrite_empty():
    r = _SeqRouter(["   \n"])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK


def test_critique_keeps_original_when_behavioral_rewrite_drops_marker():
    r = _SeqRouter(["# tvastr-kind: behavioral\nassert get() == msgs\n"])  # no marker
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _WEAK


def test_critique_allows_explicit_crash_downgrade():
    r = _SeqRouter([_CRASH])
    out = _critique_reproducer(_WEAK, _pattern(), _root_cause(), "", r)
    assert out == _CRASH  # honored — no marker required for a crash repro


def test_synthesize_runs_critique_on_behavioral():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_WEAK, _STRONG])  # synth -> weak; critique -> strong
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, r)
    assert repro.code == _STRONG
    assert repro.kind == ReproducerKind.BEHAVIORAL
    assert r.tasks == [TaskType.FIX_GENERATION, TaskType.REPRO_CRITIQUE]


def test_synthesize_skips_critique_on_crash():
    from tvastr.llm.router import TaskType
    r = _SeqRouter([_CRASH])  # only the synth response — critique must not be called
    repro = synthesize_reproducer(_pattern(), _root_cause(), [], None, r)
    assert repro.kind == ReproducerKind.CRASH
    assert r.tasks == [TaskType.FIX_GENERATION]  # critique skipped
```

> NOTE: the synth call in `synthesize_reproducer` uses `TaskType.FIX_GENERATION` today (unchanged). If the file's existing `_pattern()`/`_root_cause()` helpers differ in name, reuse whatever it already defines — only the `_SeqRouter`, `_WEAK`/`_STRONG`/`_CRASH` constants, and the new imports are added here.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repro.py -k "critique or synthesize_runs or synthesize_skips" -v`
Expected: FAIL — `ImportError: cannot import name '_critique_reproducer'`; synth doesn't call the critique yet.

- [ ] **Step 3: Add `_CRITIQUE_SYSTEM` and `_critique_reproducer`**

In `src/tvastr/verification/repro.py`, ensure the imports include `TaskType` (already imported) and add `_CRITIQUE_SYSTEM` + the function after `_build_prompt` (and after `_strip_fences`):

```python
_CRITIQUE_SYSTEM = (
    "You audit a Python reproducer used to verify a bug fix. The reproducer must "
    "FAIL on a fix that merely SUPPRESSES the error and PASS only when the intended "
    "behavior is restored. If a fix that just suppresses the exception (returns "
    "empty/default/None without restoring the real result) would STILL pass its "
    "assertions, the oracle is TOO WEAK: rewrite it to set up REAL valid input, "
    "exercise the full operation end-to-end, and assert the output matches that "
    "input (round-trip) or a documented expected value. Do NOT mock/hand-construct "
    "the already-broken state and assert it. Keep the first line "
    "`# tvastr-kind: behavioral` and end with the exact marker print. If the "
    "reproducer is already strong, return it UNCHANGED. Respond with ONLY Python "
    "source - no prose, no markdown fences."
)


def _critique_reproducer(
    code: str,
    pattern: FailurePattern,
    root_cause: RootCause,
    code_context: str,
    router: HybridRouter,
) -> str:
    """One adversarial pass that strengthens a weak behavioral reproducer.

    Returns the (possibly rewritten) code. On any failure, or a malformed
    rewrite, returns ``code`` unchanged — the critique strengthens, never blocks.
    """
    code_section = (
        f"Source of suspected files:\n{_truncate(code_context)}\n\n"
        if code_context.strip()
        else ""
    )
    prompt = (
        f"Failure: {pattern.title}\n"
        f"Root cause: {root_cause.summary}\n\n"
        f"{code_section}"
        f"Reproducer under audit:\n{code}\n\n"
        "Audit it. If a suppress-only fix would still pass its assertions, rewrite "
        "it to assert the intended behavior; otherwise return it unchanged."
    )
    try:
        response, _ = router.run(
            TaskType.REPRO_CRITIQUE,
            prompt,
            sensitivity=pattern.sensitivity,
            system=_CRITIQUE_SYSTEM,
        )
    except Exception as exc:
        log.warning("verify.repro.critique_failed", error=str(exc))
        return code
    revised = _strip_fences(response.text)
    if not revised.strip():
        return code
    # A behavioral rewrite that dropped the success marker is malformed — keep the
    # original. An explicit downgrade to `# tvastr-kind: crash` is allowed.
    if _parse_kind(revised) == ReproducerKind.BEHAVIORAL and BEHAVIOR_OK_MARKER not in revised:
        log.info("verify.repro.critique_discarded", reason="behavioral rewrite lost the marker")
        return code
    log.info("verify.repro.critiqued", changed=(revised != code))
    return revised
```

- [ ] **Step 4: Wire the critique into `synthesize_reproducer`**

In `synthesize_reproducer`, the Claude-path tail currently reads:

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

Replace it with (insert the critique between parse and return):

```python
    code = _strip_fences(response.text)
    kind = _parse_kind(code)
    if kind == ReproducerKind.BEHAVIORAL:
        code = _critique_reproducer(code, pattern, root_cause, resolved_context, router)
        kind = _parse_kind(code)  # re-parse: a rewrite keeps or restates the tag
    log.info("verify.repro.synthesized", lines=code.count("\n") + 1, model=response.model, kind=kind.value)
    return Reproducer(
        source=ReproducerSource.CLAUDE,
        code=code,
        expected_exception=pattern.exception_type,
        kind=kind,
    )
```

(`resolved_context` is the already-computed `code_context() if callable else code_context` value in `synthesize_reproducer`. If the local variable has a different name, pass that one.)

- [ ] **Step 5: Run the critique tests to verify they pass**

Run: `uv run pytest tests/test_repro.py -k "critique or synthesize_runs or synthesize_skips" -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean. (Existing repro/verifier tests use an `_ScriptedLLM` that returns the same text for every call, so the added critique call returns the same behavioral code — a no-op there.)

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/verification/repro.py tests/test_repro.py
git commit -m "feat(verify): self-critique gate strengthens weak behavioral reproducers"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With live mode + Docker, run #21896 → Verify. Confirm the reproducer now asserts message **round-trip** (not `== []`), and the verdict is **`masks_symptom`** (the masking fix fails the strengthened assertion) instead of `verified_via_behavior`. The timeline should show a `router.decide`/`llm.call` for `repro_critique`.

## Self-Review (completed by author)

- **Spec coverage:** `REPRO_CRITIQUE` cloud task (Task 1 + `test_repro_critique_is_a_cloud_task`); prompt hardening — forbid degraded-state assertion / round-trip / suppress-must-fail (Task 1 Step 4 + `test_system_prompt_forbids_asserting_degraded_state`); `_critique_reproducer` one-pass, behavioral-only gating (Task 2 wiring + `test_synthesize_runs_critique_on_behavioral` / `test_synthesize_skips_critique_on_crash`); graceful on error (`test_critique_graceful_on_error`); empty/marker-less guard + crash-downgrade allowance (`test_critique_keeps_original_*` / `test_critique_allows_explicit_crash_downgrade`); regression — same-text ScriptedLLM no-op (Step 6); live metric.
- **Placeholder scan:** none — every code/test step is complete; the one NOTE gives a concrete reuse instruction, not a TODO.
- **Type consistency:** `_critique_reproducer(code, pattern, root_cause, code_context, router) -> str`; `TaskType.REPRO_CRITIQUE`; gating on `ReproducerKind.BEHAVIORAL`; re-parse via `_parse_kind`; guard via `BEHAVIOR_OK_MARKER`; synth call stays `TaskType.FIX_GENERATION` (asserted in the `_SeqRouter` task-sequence tests).
