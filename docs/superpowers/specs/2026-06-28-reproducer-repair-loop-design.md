# Design Spec: Sandbox-grounded reproducer repair loop

**Date:** 2026-06-28
**Branch:** `feature/reproducer-repair-loop` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

On llama_index #17105 the agent now reliably produces the correct diagnosis
(confidence 0.9, `same_root_cause=True` vs PR #21543 across runs — issue-era
retrieval + the multi-object parse fix hold). But **verify is flaky**: one run
returned `verified_via_reproducer`, the re-run returned `repro_broken`. The
cause is reproducer fidelity — the synthesizer hand-rolls a fake of an *external*
SDK object:

```python
class FakeOllamaResponse:        # synthesized fake
    def __getitem__(self, key): return self._data[key]   # dict-style, no __iter__/keys()
```

The fix's `get_additional_kwargs` does `dict(response)`; `dict()` on this fake
(subscript-only, no `keys`/`__iter__`) falls back to the sequence protocol →
`__getitem__(0)` → `KeyError: 0` → crash **in the fake**, not real code →
`repro_broken`. Meanwhile the *real* `ollama` package **is installed in the
sandbox** (dependency provisioning installed `llama-index-multi-modal-llms-ollama`,
which pulls in `ollama`), so the reproducer could have imported and constructed a
real `GenerateResponse` (a pydantic model that supports `dict()`/`model_dump()`)
— exactly the manual step Claude used ("built a venv with `ollama==0.4.2` and
constructed a real `GenerateResponse`").

Root cause: the reproducer is synthesized one-shot, **before** provisioning and
**without running it**, so it mocks external types instead of using the real,
already-available ones, and nothing catches the unfaithful fake until the verdict.

## Goal & success criterion

Ground the reproducer in the real provisioned dependencies via a bounded
**synth → run → classify → repair** loop in the provisioned sandbox, so the
reproducer faithfully reproduces the issue's symptom on real objects before it is
trusted.

**Success:** re-run #17105 — the reproducer imports/constructs a real
`GenerateResponse` (or, if a draft fakes it and breaks, the loop repairs it from
the real error), the baseline reproduces the actual `AttributeError`, the patch's
`dict(response)` works on the real object, and `verify.result` is **stably**
`verified_via_reproducer` across runs (no `repro_broken` from fake-vs-fix
interactions).

## Decisions (locked in brainstorming, corrected during planning)

1. **Sandbox-grounded repair loop** (over introspection-assisted one-shot or a
   prompt-only tweak): provision first, then run the full verify cycle and repair
   the reproducer from the real error when it proves untrustworthy. Realizes the
   recorded dynamic-reproduction frontier, scoped to the reproducer.
2. **Repair trigger = a `REPRO_BROKEN` outcome of the FULL cycle**, not a baseline
   check. *(Correction:* validating only the baseline misses #17105 — the fake
   `GenerateResponse` reproduces correctly at baseline, `base.py` raising the real
   `AttributeError`; it only breaks at **rerun** under the fix's `dict(response)`.
   The existing `REPRO_BROKEN` verdict is exactly "the reproducer broke in its own
   scaffolding rather than real code," and it fires at rerun for the fake case and
   at baseline for import/construction errors. So the loop wraps
   baseline→patch→rerun→triage and retries on `REPRO_BROKEN`.)*
3. **Bounded** `_MAX_REPRO_REPAIR = 2`; gated by `verify_repro_repair` (default
   on); on exhaustion return the last `REPRO_BROKEN`; flag-off → today's flow.

## Architecture

Provisioning is reordered to run **before** synth so the cycle executes against
the real installed deps. The baseline→patch→rerun→triage is extracted into a
helper `_run_cycle(handle, repro, fix, ...) -> _CycleOutcome(verdict, oracle,
evidence)` (the existing logic, returning the outcome instead of finishing). The
verify method loops it, repairing the reproducer when the outcome is
`REPRO_BROKEN`:

```
prepare → provision (real deps installed)
repro = synthesize_reproducer(...)          # _SYSTEM strengthened: import REAL deps, never fake SDK types
for attempt in 0.._MAX_REPRO_REPAIR:        # default 2 repairs
    handle.write_file("repro.py", repro.code)
    outcome = self._run_cycle(handle, repro, fix, pr_number, pr_files, started)
    if outcome.verdict != REPRO_BROKEN or not self.repro_repair or attempt == _MAX_REPRO_REPAIR:
        return self._finish(handle, outcome.verdict, outcome.oracle, started, outcome.evidence)
    # REPRO_BROKEN → the reproducer broke in its own scaffolding, not real code.
    emit verify.repro_repair {attempt, error_tail: outcome.evidence["rerun_stderr_tail"]}
    repro = repair_reproducer(repro, outcome.evidence, pattern, root_cause, self.ctx.router)
```

`_run_cycle` is exactly today's baseline → (overlay) → patch → rerun → triage →
scoped-tests path, but returns `_CycleOutcome` rather than calling `_finish`. The
patch is re-applied each attempt (idempotent: it overwrites the module). The
overlay path is inside the cycle, unchanged.

**Why `REPRO_BROKEN` is the right trigger:** it is the existing verdict for "the
rerun failed in a way the reproducer can't be trusted for" — a non-zero rerun
that does NOT carry the original exception (crash kind), or a behavioral rerun
that neither asserted-OK nor failed cleanly. That is precisely the fake-breaks-
under-the-fix case (#17105 rerun `KeyError: 0` in `/work/repro.py`) and the
baseline/rerun import-error case. A real `VERIFIED_*`/`STILL_BROKEN`/
`MASKS_SYMPTOM`/`NO_REPRO`/`REGRESSION` outcome is returned immediately (no
repair).

## The repair step

`repair_reproducer(repro, evidence, pattern, root_cause, router) -> Reproducer`
(a new `repro.py` function): prompt the model with the failing reproducer code +
the real rerun stderr (from `evidence`) + "This reproducer FAILED in its own code,
not in the library under test — it is untrustworthy. The issue's integration and
its dependencies are installed in this sandbox; import and construct the REAL
objects (e.g. `from ollama import …`) instead of fake/stub classes for external
library types (fakes won't match real behavior — e.g. a hand-rolled response that
supports `[]` but not `dict()`). Return only the corrected reproducer." Reuses
the kind tag + marker conventions and `_strip_fences`/`_parse_kind`; degrade
(return the original `Reproducer`) on any error.

## `_SYSTEM` strengthening (the first draft)

Add to `repro.py`'s `_SYSTEM`: "The issue's integration package and its
dependencies are installed in the run sandbox. Import and construct the REAL
classes named in the traceback (e.g. response/SDK objects) — do NOT define
fake/stub classes for external library types; fakes won't match real behavior
(e.g. a hand-rolled response that supports `[]` but not `dict()`)." So the draft
tends to be faithful before any repair.

## Components

| File | Change |
|------|--------|
| `verification/verifier.py` | reorder provision before synth; extract `_run_cycle(...) -> _CycleOutcome(verdict, oracle, evidence)` from the existing baseline→overlay→patch→rerun→triage→scoped-tests path (returning the outcome, not `_finish`-ing); wrap it in the repair loop in `verify()`; emit `verify.repro_repair`; gate on `self.repro_repair`; on flag-off / exhaustion, return the (last) outcome. `_CycleOutcome` is a small local dataclass/namedtuple. |
| `verification/repro.py` | `repair_reproducer(repro, evidence, pattern, root_cause, router) -> Reproducer`; strengthen `_SYSTEM` (import real installed deps; never fake external SDK/response types). |
| `config.py` | `verify_repro_repair: bool = True` (`TVASTR_VERIFY_REPRO_REPAIR`). |
| `tests/conftest.py` | seal `TVASTR_VERIFY_REPRO_REPAIR=false`. |
| `api/templates/app.html` | summary label for `verify.repro_repair` (Minor). |

`Verifier.__init__` gains `repro_repair: bool = True` (mirrors `provision_deps`/
`source_overlay`); the verify route passes `repro_repair=settings.verify_repro_repair`.

## Interaction with existing features

- **Provisioning** must run before synth (reorder); it already runs before
  baseline, so this only moves it ahead of synth.
- **Overlay** (no_repro path) is unchanged: if the loop ends `RAN_CLEAN`
  (baseline didn't reproduce), the existing no_repro → overlay → re-baseline path
  runs. (If desired, the overlaid re-baseline can reuse the same classifier;
  kept out of scope to limit blast radius — overlay already re-runs baseline.)
- **Static critique** (`_critique_reproducer`) is complementary and unchanged:
  it hardens the behavioral *assertion*; this loop ensures the repro *runs on
  real deps and reproduces the symptom*. Both apply.
- **Register-aware** synth/verdict unchanged: the loop validates whatever
  register's reproducer was synthesized.

## Error handling (never crashes)

- The provision-before-synth **reorder is unconditional**; only the repair *loop*
  is flag-gated. `synthesize_reproducer` does not depend on the sandbox, so
  reordering it after provision yields the same `Reproducer`. With
  `verify_repro_repair=false` the loop is skipped — synth runs once, then a single
  baseline run — functionally equivalent to today (same reproducer, same baseline).
- `repair_reproducer` LLM/parse failure → return the previous draft (no crash).
- Budget exhausted without REPRODUCES_SYMPTOM → use the last run as the baseline
  and fall through to the existing verdict triage (`REPRO_BROKEN`/`NO_REPRO`).
- Any sandbox/LLM exception caught and logged; degrade.

## Observability

`verify.repro_repair` event per repair (payload `{attempt, error_tail}`),
emitted before each retry, rendered in the dashboard timeline.

## Scope boundary (deferred, recorded follow-up)

- **Version-pinning the external dep** to the issue's reported version (e.g.
  `ollama==0.4.2`) — the provisioned version usually still reproduces; pin only
  when it doesn't. The remaining slice of the dynamic-reproduction frontier.

## Testing (offline)

- Repair loop over a fake sandbox with scripted `RunResult`s: first cycle yields
  `REPRO_BROKEN` (rerun non-zero, no original exception) → `repair_reproducer`
  called → second cycle yields `VERIFIED_*` → that verdict is returned.
- First cycle yields `VERIFIED_*`/`STILL_BROKEN`/`MASKS_SYMPTOM`/`NO_REPRO` →
  returned immediately, no repair.
- Budget exhausted (every cycle `REPRO_BROKEN`) → returns `REPRO_BROKEN` after
  `_MAX_REPRO_REPAIR` repairs; `verify.repro_repair` emitted per repair.
- `verify_repro_repair=false` → no loop (single cycle), every existing verdict
  outcome unchanged (the extracted `_run_cycle` preserves them).
- `repair_reproducer` prompt content (mock router); returns the original
  `Reproducer` on LLM/parse error.
- Live metric: #17105 → stable `verified_via_reproducer` across repeated runs
  (the fake gets repaired to real objects; no `repro_broken` from fake-vs-fix).
