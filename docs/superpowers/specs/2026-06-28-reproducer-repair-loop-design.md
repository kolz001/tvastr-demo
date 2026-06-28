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

## Decisions (locked in brainstorming)

1. **Sandbox-grounded repair loop** (over introspection-assisted one-shot or a
   prompt-only tweak): provision first, then synth → run → classify → repair in
   the provisioned sandbox until the reproducer reproduces the issue's symptom.
   Realizes the recorded dynamic-reproduction frontier, scoped to the reproducer.
2. **Validation predicate = matches the issue's symptom.** A baseline failure is
   "the real bug" when its error matches the issue's expected symptom
   (`pattern.exception_type` / `representative_message`); a non-matching
   scaffolding error (`ImportError`/`ModuleNotFoundError`/`NameError`/a
   `TypeError`/`KeyError` in the repro's own construction) triggers repair.
3. **Bounded** `_MAX_REPRO_REPAIR = 2`; gated by `verify_repro_repair` (default
   on); degrade to today's synth+baseline+verdict flow on exhaustion or flag-off.

## Architecture

```
prepare → provision (real deps installed)
draft = synthesize_reproducer(...)          # _SYSTEM strengthened: import REAL deps, never fake SDK types
for attempt in 0.._MAX_REPRO_REPAIR:        # default 2 repairs
    result = handle.run(["python", "repro.py"])     # in the provisioned sandbox
    klass = classify(result, pattern, repro)
    emit verify.repro_repair {attempt, classification: klass, error_tail}
    if klass == REPRODUCES_SYMPTOM:  break          # VALIDATED → this is the baseline
    if klass == RAN_CLEAN:           break          # no symptom → existing NO_REPRO/overlay path
    # klass == SCAFFOLDING_ERROR:
    draft = repair_reproducer(draft, result.stderr, expected, pattern, root_cause, router)
    handle.write_file("repro.py", draft)
# the loop's last `result` is the baseline; proceed: patch → rerun → verdict (unchanged)
```

This loop **subsumes** the current baseline run (its final `result` is the
baseline). Provisioning is reordered to run **before** synth so the loop executes
against the real installed deps. `code_context` (source of suspected files) is
unchanged and still seeds synth.

## The classifier

`classify(result, pattern, repro) -> {REPRODUCES_SYMPTOM, SCAFFOLDING_ERROR, RAN_CLEAN}`:

- **REPRODUCES_SYMPTOM** — the failure matches the issue's expected symptom:
  - crash repro: `pattern.exception_type` (or `repro.expected_exception`) appears
    in stderr, OR a representative-message token matches;
  - behavioral repro: the run failed via the assertion path (`AssertionError`)
    rather than a scaffolding error (i.e. `_reproduced(result, repro)` True AND
    stderr is not a scaffolding error).
- **SCAFFOLDING_ERROR** — non-zero exit whose stderr is an import/name/construction
  error (`ImportError`, `ModuleNotFoundError`, `NameError`, `TypeError`/`KeyError`
  inside the reproducer file) and does NOT match the symptom.
- **RAN_CLEAN** — exit 0 / no failure (behavioral marker present, or crash repro
  succeeded) → no symptom to repair; hand to the existing no-repro/overlay path.

This also closes a latent weakness: today a scaffolding crash at baseline (exit
≠ 0) masquerades as "reproduced"; the classifier now distinguishes it.

## The repair step

`repair_reproducer(code, error_stderr, expected, pattern, root_cause, router) -> str`
(a new `repro.py` function): prompt the model with the failing reproducer + the
real stderr + "This error is scaffolding, NOT the issue's symptom (`<expected>`).
The issue's integration and its dependencies are installed in this sandbox —
import and construct the REAL objects (e.g. `from ollama import …`); never define
fake/stub classes for external library types. Return only the corrected
reproducer." Reuses the kind tag + marker conventions; degrade (return the
original) on any error.

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
| `verification/verifier.py` | reorder: `prepare` → provision → synth → `_reproduce_with_repair(handle, repro, pattern, root_cause)` (the loop; returns the validated baseline `RunResult` + final `Reproducer`) → existing patch/rerun/verdict. Emit `verify.repro_repair`. Gate on `self.repro_repair`. Degrade to a single baseline run when off / on exhaustion. |
| `verification/repro.py` | `repair_reproducer(...)`; a `classify_reproduction(result, pattern, repro) -> str` helper (or inline in the verifier); strengthen `_SYSTEM`. |
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

`verify.repro_repair` event per attempt (payload `{attempt, classification,
error_tail}`), rendered in the dashboard timeline.

## Scope boundary (deferred, recorded follow-up)

- **Version-pinning the external dep** to the issue's reported version (e.g.
  `ollama==0.4.2`) — the provisioned version usually still reproduces; pin only
  when it doesn't. The remaining slice of the dynamic-reproduction frontier.

## Testing (offline)

- Repair loop over a fake sandbox with scripted `RunResult`s: scaffolding-error
  first → `repair_reproducer` called → REPRODUCES_SYMPTOM on retry → validated;
  symptom on first try → no repair; budget exhausted → degrade to verdict triage.
- `classify_reproduction`: symptom-match (crash + behavioral) vs scaffolding
  (`ImportError`/`KeyError`-in-repro) vs ran-clean.
- `repair_reproducer` prompt content (mock router); returns original on error.
- `verify_repro_repair=false` → no reorder/loop (single baseline), behavior
  byte-equivalent.
- Live metric: #17105 → stable `verified_via_reproducer` across repeated runs
  (no `repro_broken` from fake-vs-fix).
