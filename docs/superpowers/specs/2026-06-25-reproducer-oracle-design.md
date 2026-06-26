# Design Spec: Reproducer oracle correctness

**Date:** 2026-06-25
**Branch:** `feature/reproducer-oracle` (off `main`)
**Status:** Approved (user waived the written-spec review gate)

## Problem

Behavioral verification is only as trustworthy as what the reproducer asserts.
On llama_index #21896 the synthesized behavioral reproducer mocked an
already-degraded node (`metadata={}`) and asserted the degraded output
(`assert result == []`) — so the agent's symptom-masking fix (`.get('sub_dicts',
[])`, which returns `[]`) **passed** → a false `verified_via_behavior`. The model
reasoned from the *crash symptom* ("don't crash on an empty node") instead of the
*user-facing functionality* ("real messages survive the round-trip"). The flaw is
in the reproducer's setup+assertion: it constructs the broken intermediate state
and asserts the broken result.

## Goal & success criterion

Make the behavioral reproducer assert the *intended* behavior, so a
symptom-masking fix fails it.

**Success:** re-running #21896's Verify yields `masks_symptom` (not
`verified_via_behavior`) — the strengthened reproducer asserts message round-trip
and the (already-merged) patch-path fix makes the masking patch take effect, so
the rerun's assertion fails.

## Scope

**In scope:** harden the synthesis prompt against asserting the degraded state,
and add a one-pass self-critique gate that strengthens a weak behavioral
reproducer. Contained to `verification/repro.py` + one router task type.

**Out of scope:** the verifier, sandbox, agent graph (all unchanged); peeking at
the maintainer PR to derive expected behavior (would cheat the benchmark);
multi-pass critique loops.

## Decisions (locked in brainstorming)

1. **Both** prompt hardening AND a self-critique gate.
2. **Always run** the critique on behavioral reproducers (not flag-gated) — the
   critique is a correctness guard on the verify verdict, not an optional
   enhancement; gating it off would ship a known false-green path.
3. **One pass**, Claude behavioral repros only (crash + `ISSUE_BODY` + mock skip
   it naturally).

## Architecture

Two coordinated changes inside `synthesize_reproducer`:

**1. Prompt hardening (`_SYSTEM`).** Add the observed anti-patterns:
- Do NOT mock/hand-construct the already-broken intermediate state and assert it.
- Set up REAL, valid input; exercise the FULL operation end-to-end; assert the
  user-visible output MATCHES that input (round-trip) or a stated expected value.
- Asserting an empty/degenerate result is FORBIDDEN unless empty is genuinely
  correct.
- A fix that only suppresses the error must FAIL the assertion.

**2. Self-critique gate (one pass).** After the Claude-path synthesis produces a
`BEHAVIORAL` reproducer, a second LLM call (`TaskType.REPRO_CRITIQUE`)
adversarially reviews it: *"Would a fix that merely suppresses the error — without
restoring behavior — still pass these assertions? If yes, the oracle is too weak:
rewrite it to set up real input and assert the output reflects that input.
Otherwise return it unchanged."* The returned (possibly rewritten) code is
re-parsed for its `# tvastr-kind:` tag and used.

**Gating:** the critique runs **iff** the synthesized reproducer's kind is
`BEHAVIORAL`. This skips crash reproducers, the `ISSUE_BODY` fast-path, and mock
mode (the `MockClaudeClient` returns prose → parses as `CRASH`).

## Components

| File | Change |
|------|--------|
| `src/tvastr/llm/router.py` | `TaskType.REPRO_CRITIQUE = "repro_critique"` (cloud task — redacted, own audit events). |
| `src/tvastr/verification/repro.py` | hardened `_SYSTEM`; new `_CRITIQUE_SYSTEM`; new `_critique_reproducer(code, pattern, root_cause, code_context, router) -> str`; `synthesize_reproducer` runs the critique on behavioral Claude repros. |
| `tests/test_repro.py` | critique strengthens weak / leaves strong / only-on-behavioral / graceful-degradation / guard; prompt-content; new task type. |

**`_critique_reproducer`:** one `router.run(TaskType.REPRO_CRITIQUE, prompt,
sensitivity=pattern.sensitivity, system=_CRITIQUE_SYSTEM)`; prompt embeds the
synthesized reproducer + issue/root-cause/code context; returns
`_strip_fences(response.text)`. Wrapped in try/except → on any failure, return
the original code unchanged. Guard: if the rewrite is empty or no longer contains
`BEHAVIOR_OK_MARKER`, keep the original.

**`synthesize_reproducer` wiring** (after existing Claude synth + `_parse_kind`):
```python
code = _strip_fences(response.text)
kind = _parse_kind(code)
if kind == ReproducerKind.BEHAVIORAL:
    code = _critique_reproducer(code, pattern, root_cause, resolved_context, router)
    kind = _parse_kind(code)
return Reproducer(source=ReproducerSource.CLAUDE, code=code,
                  expected_exception=pattern.exception_type, kind=kind)
```
(The `ISSUE_BODY` early return is untouched.)

## Data flow (#21896)

```
synth → weak behavioral: mock empty node; assert result == []
critique → "a []-returning suppress-only fix would PASS this; too weak. Rewrite:
            put REAL messages, get them back, assert round-trip."
        → strong behavioral: mem.put(messages); assert mem.get(q) == messages; print(MARKER)
verifier baseline → reproduced (fails)
verifier rerun (masking fix, patch now takes effect) → got == [] ≠ messages → AssertionError
        → MASKS_SYMPTOM ✅  (false green eliminated)
```

## Edge cases / safety

- **Critique call fails** → try/except returns the original reproducer (never blocks).
- **Rewrite empty / drops the marker** → keep the original (guard).
- **Critique downgrades to crash** (intended behavior genuinely unassertable) →
  re-parse yields `CRASH`; honored (honest fallback, same as the synth prompt's).
- **Mock mode** → mock prose parses as `CRASH` → critique never fires; existing
  mock-path tests untouched.
- **Cost** → +1 cloud call, only on Claude behavioral repros.

## Testing (TDD, offline — scripted router returning a response sequence)

- **`_critique_reproducer` unit:** stub returns strengthened → returned; stub
  raises → original returned; stub returns empty/marker-less → original kept.
- **End-to-end strengthening:** 2-response router (call 1 = weak behavioral; call
  2 = strong round-trip behavioral) → final reproducer is the strong one, kind
  `BEHAVIORAL`. (Regression for #21896's weak oracle.)
- **Critique only on behavioral:** crash-kind synth → critique not called (1 call);
  behavioral synth → critique called (2 calls).
- **Prompts:** `_SYSTEM` contains the round-trip / forbid-degraded-assertion
  instructions; `_CRITIQUE_SYSTEM` contains the "would a suppress-only fix pass?"
  instruction.
- **Router:** `TaskType.REPRO_CRITIQUE` exists, cloud task (not in `_LOCAL_TASKS`).
- **Regression:** existing `test_repro.py` + verifier tests stay green (the
  `_ScriptedLLM` returns the same text for both calls, so the critique is a no-op
  there).
- **Live metric:** #21896 Verify → `masks_symptom`.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/llm/router.py` | `REPRO_CRITIQUE` task type |
| `src/tvastr/verification/repro.py` | hardened prompt + critique gate |
| `tests/test_repro.py` | critique + prompt + task-type tests |
