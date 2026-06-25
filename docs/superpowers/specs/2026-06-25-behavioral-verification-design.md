# Design Spec: Behavioral verification (positive-postcondition reproducer)

**Date:** 2026-06-25
**Branch:** `feature/behavioral-verification` (off `main`)
**Status:** Approved design — ready for implementation plan

## Problem

The verify-fix loop's reproducer is **crash-oriented**: `synthesize_reproducer`
builds a script that re-triggers `expected_exception`, and the oracle
(`_original_exception_seen`) only asks "does the original exception still fire?"
So `VERIFIED_VIA_REPRODUCER` literally means "the original exception no longer
fires." This lets a **symptom-masking** fix pass: on llama_index #21896, the
agent's fix changed `node.metadata['sub_dicts']` to `.get('sub_dicts', [])` —
the `KeyError` stops, the reproducer sees no exception, verdict is green — yet
chat history is silently lost (the maintainer's fix serializes/deserializes
`sub_dicts` to actually restore it). The benchmark graded the agent's fix
`partial` ("a weaker partial solution… only suppresses the KeyError"), but the
verify loop would call it **verified**. Nothing asserts behavior was restored.

## Goal & success criterion

Catch symptom-masking by upgrading the reproducer from "exception no longer
fires" to "the operation produces a correct, non-degenerate result," with an
honest fallback when expected behavior can't be inferred.

**Success:** re-running #21896 end-to-end reports **`masks symptom`** on the
agent's masking fix instead of a green verdict; a real (behavior-restoring) fix
reports `verified_via_behavior`. (#21897 is a separate *diagnosis* problem, out
of scope.)

## Scope

**In scope:** a behavioral-preferred reproducer (setup → exercise → assert
expected result → success sentinel) and the verifier triage that distinguishes
behavior-restored / masks-symptom / still-broken, plus two new verdicts and UI
badges.

**Out of scope:** full functional-correctness oracles / property testing;
LLM-judge-only (no execution) checks; changing the agent graph, router, or the
diagnosis stage.

## Decisions (locked in brainstorming)

1. **Target symptom-masking specifically** via a positive postcondition; honest
   fallback to today's crash-trigger when no behavior is inferable.
2. **One behavioral-preferred reproducer**, self-reporting its kind (behavioral
   vs crash) — not a second reproducer.
3. **Two new verdicts:** `VERIFIED_VIA_BEHAVIOR` (strongest green) and
   `MASKS_SYMPTOM` (not green); keep `VERIFIED_VIA_REPRODUCER` for the crash
   fallback (now explicitly "crash-only").

## Architecture — the sentinel discriminator

A behavioral reproducer ends, after its assertions, with a success sentinel it
only reaches if every assertion passed:
```python
# ... setup; exercise the buggy operation; assert <expected result> ...
print("TVASTR_BEHAVIOR_OK")
```
The verifier distinguishes the post-patch rerun deterministically:

| Post-patch rerun | Detection | Verdict |
|---|---|---|
| sentinel in stdout (exit 0) | assertions passed | `VERIFIED_VIA_BEHAVIOR` (green) |
| original exception in stderr | `_original_exception_seen` | `STILL_BROKEN` |
| no sentinel, no original exc, `AssertionError` in stderr | behavior wrong | `MASKS_SYMPTOM` (not green) |
| no sentinel, some other exception | repro itself broke | `REPRO_BROKEN` |

`Reproducer.kind` selects which post-patch logic runs. The **crash-trigger
fallback** path is unchanged (uses `expected_exception`, yields
`VERIFIED_VIA_REPRODUCER`). Baseline "reproduced?" for a behavioral repro =
**sentinel absent pre-patch**; if the baseline already prints the sentinel, the
bug didn't reproduce → `NO_REPRO`. Green requires **exit 0 AND sentinel** (a
crash after printing still fails).

## Components

| File | Change |
|------|--------|
| `verification/models.py` | `Verdict.VERIFIED_VIA_BEHAVIOR`, `Verdict.MASKS_SYMPTOM`; `is_green` adds `VERIFIED_VIA_BEHAVIOR` (NOT `MASKS_SYMPTOM`); `ReproducerKind` StrEnum (`BEHAVIORAL`/`CRASH`); `Reproducer.kind: ReproducerKind = CRASH`; module constant `BEHAVIOR_OK_MARKER = "TVASTR_BEHAVIOR_OK"`. |
| `verification/repro.py` | New prompt: write a behavioral reproducer (exercise + `assert` + end with `print(BEHAVIOR_OK_MARKER)`); fall back to a crash-trigger script when expected behavior can't be determined. Claude tags the first line `# tvastr-kind: behavioral` or `# tvastr-kind: crash`; `synthesize_reproducer` parses it (default `CRASH` if absent) and sets `Reproducer.kind`. The no-Claude `ISSUE_BODY` fast-path stays `CRASH`. |
| `verification/verifier.py` | After rerun, branch on `repro.kind`: `CRASH` → existing logic untouched; `BEHAVIORAL` → sentinel triage (table above). Behavioral baseline "reproduced" = sentinel absent. |
| `api/templates/app.html` | `VERDICT_BADGE`: `verified_via_behavior` → green "verified · behavior"; `masks_symptom` → red "masks symptom". |

**Oracle field:** behavioral verdicts (incl. `MASKS_SYMPTOM`) record
`oracle="behavior"`; the crash fallback keeps `oracle="reproducer"`.

**Unchanged:** scoped-tests regression (still runs only after a green
reproducer — now either behavioral or crash green), sandbox, router, agent graph.

## Data flow (#21896)

```
behavioral repro: m=VectorMemory(...); m.put(msgs); assert m.get()==msgs; print(MARKER)
baseline (pre-patch): KeyError('sub_dicts') → no sentinel → reproduced ✅
agent masking fix (.get('sub_dicts', [])):
  rerun → no KeyError, m.get()==[] → AssertionError, no sentinel → MASKS_SYMPTOM  (was false green)
maintainer real fix (serialize/deserialize):
  rerun → m.get()==msgs → sentinel → VERIFIED_VIA_BEHAVIOR ✅
```

## Edge cases / honesty

- **No inferable behavior:** Claude tags `crash` → existing path,
  `VERIFIED_VIA_REPRODUCER` (crash-only). Never forces a behavioral assert where
  none exists.
- **LLM-authored oracle is wrong** (bad assertion fails even on a correct fix) →
  false `MASKS_SYMPTOM`. Mitigations: only trust the behavioral verdict when the
  baseline reproduced (sentinel absent pre-patch); the reproducer code is
  streamed to the UI (`verify.repro_synth`) for human inspection; crash fallback
  remains for low-confidence cases. Documented as the known risk of
  LLM-authored oracles.
- **Library raises its own `AssertionError`** in the exercised path: conflated
  with our assert (both mean "behavior wrong") — acceptable.
- **Sentinel printed then a later crash:** green requires exit 0 AND sentinel, so
  this still fails.
- **Mock mode / ISSUE_BODY:** stay `CRASH`; existing verifier tests unaffected.

## Testing (TDD, offline — `_FakeSandbox` scripted RunResults + stub router)

- **models.py:** `is_green` includes `VERIFIED_VIA_BEHAVIOR`, excludes
  `MASKS_SYMPTOM`; new verdicts + `ReproducerKind` + `BEHAVIOR_OK_MARKER` exist.
- **repro.py:** stub returns `# tvastr-kind: behavioral` → `kind==BEHAVIORAL`;
  `crash`/untagged → `CRASH`; the behavioral prompt contains the marker + assert
  + fallback instructions.
- **verifier behavioral triage (core):** baseline-fail + rerun sentinel/exit0 →
  `VERIFIED_VIA_BEHAVIOR` (green, oracle="behavior"); rerun AssertionError/no
  sentinel/no original exc → `MASKS_SYMPTOM` (not green) *(regression for
  #21896)*; rerun original exception → `STILL_BROKEN`; rerun other exception →
  `REPRO_BROKEN`; baseline already prints sentinel → `NO_REPRO`.
- **crash-path regression:** a `CRASH` reproducer still yields
  `VERIFIED_VIA_REPRODUCER`/`STILL_BROKEN` exactly as today.
- **UI:** two `VERDICT_BADGE` entries present; app boots.
- **Live metric:** #21896 reports `masks symptom` on the agent fix.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/verification/models.py` | verdicts, `is_green`, `ReproducerKind`, `Reproducer.kind`, `BEHAVIOR_OK_MARKER` |
| `src/tvastr/verification/repro.py` | behavioral prompt + kind parsing |
| `src/tvastr/verification/verifier.py` | behavioral post-patch triage |
| `src/tvastr/api/templates/app.html` | two verdict badges |
| `tests/test_verifier.py` (+ repro/models tests) | behavioral triage, kind parsing, is_green |
