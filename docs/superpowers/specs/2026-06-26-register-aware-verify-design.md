# Design Spec: Register-aware verification (judgment ⟂ verify resolution)

**Date:** 2026-06-26
**Branch:** `feature/register-aware-verify` (off `main`)
**Status:** Approved design — pending user review of this written spec
**Predecessor of:** `docs/superpowers/plans/2026-06-26-agent-judgment-improvement.md` (call this its Task 0)

## Problem

The agent-judgment plan will make the agent choose the right *fix register* —
e.g. on llama_index #21062 the maintainer's accepted fix (#21279) was a
non-breaking `logger.warning` on the failing field, not a behavioral repair (the
bug genuinely can't be fixed client-side). But the verify loop we just shipped
judges every fix behaviorally: it reproduces the failure and checks the fix
*resolves* it. A `WARN`/`DOCUMENT` fix doesn't change behavior, so at rerun the
reproducer still triggers the bug → `STILL_BROKEN` (crash oracle,
`verifier.py:289`) or non-`VERIFIED`/`MASKS_SYMPTOM` (behavioral oracle,
`verifier.py:314-346`). **The maintainer-correct warning fix can never go
green.** Judgment (optimizing register-match) and verify (optimizing behavioral
resolution) would contradict on exactly the bugs judgment exists to fix.

Separately, the buggy-file overlay derives its "base" (the pre-fix state) from
the fixing PR — but the base must be a fix that actually **landed on `main`**. A
proposal (open / unmerged-closed PR) is not the maintainer norm, and for the
overlay it is useless: an unmerged PR has no `merge_commit_sha`.

## Goal & success criterion

1. Verify judges a fix by the success definition of *its register*, so a
   maintainer-correct `WARN`/`BETTER_ERROR`/`DOCUMENT` fix gets an honest, fitting
   verdict instead of `STILL_BROKEN`.
2. All "base / ground-truth" derivation uses **merged-to-`main` fixes only**.

**Success:** with judgment producing a `WARN` fix for #21062, verify greens it
via the warning oracle (`VERIFIED_VIA_WARNING`); a `DOCUMENT` fix yields the
honest `UNVERIFIED_DOC_ONLY`; a genuine `REPAIR` (e.g. #15743) is unchanged. The
overlay never derives a base from an unmerged PR.

## Decisions (locked in brainstorming)

1. **Register-polymorphic oracle.** Verify selects its oracle from the fix's
   register (over routing-past-verify or a fuzzy unified "surfaced-or-fixed"
   oracle).
2. **Register-aware reproducer synthesis.** The register is realized through the
   reproducer's *assertion*, reusing the entire baseline→rerun→verdict flow
   unchanged (over a trigger-only repro + runtime capture-diff wrapper).
3. **Register provenance: `FixProposal.register`, default `REPAIR`.** Verify
   trusts the label; until the judgment plan ships, every fix is `REPAIR` →
   today's behavior exactly (zero regression). Independent re-classification of a
   mislabeled fix is future hardening, not built now.
4. **Merged-to-`main` base only, filtered at the call sites** (leave
   `discover_pr`'s open>merged>closed ranking intact for live triage).

## The register → oracle table

| Register | Synthesized assertion (the bug = assertion fails at baseline) | Green verdict |
|----------|--------------------------------------------------------------|---------------|
| `REPAIR` / `FAIL_FAST` | behavior correct (today's `BEHAVIOR_OK_MARKER` / no exception) | `VERIFIED_VIA_BEHAVIOR` / `VERIFIED_VIA_REPRODUCER` (today) |
| `WARN` | the relevant warning fires on the failing input (`warnings.catch_warnings(record=True)` + assert a warning matching the symptom/field) | `VERIFIED_VIA_WARNING` |
| `BETTER_ERROR` | a clearer error is raised — caught via a plain `try/except`, asserting the error type/message references the failing symbol and differs from the baseline's cryptic failure (stdlib only; no pytest dependency in `repro.py`) | `VERIFIED_VIA_BETTER_ERROR` |
| `DOCUMENT` | none — docs-only diff has no runtime signal; short-circuit before sandboxing | `UNVERIFIED_DOC_ONLY` |

For `WARN`/`BETTER_ERROR` the existing flow *just works*: baseline (pre-fix) fails
the register assertion (warning/clear-error absent = bug reproduces); rerun
(post-fix) passes it = verified. No new verifier control flow — only the
synthesized assertion + the verdict label.

## New verdicts (extend `Verdict`)

- `VERIFIED_VIA_WARNING` — `is_green=True` (a real verification of the warn fix).
- `VERIFIED_VIA_BETTER_ERROR` — `is_green=True`.
- `UNVERIFIED_DOC_ONLY` — honest non-green/non-red, sibling to the existing
  `UNVERIFIED_SMOKE_IMPORT_ONLY` (we couldn't behaviorally verify, but it isn't
  broken). Not counted in `is_green`.

This continues the enum's "make the *kind* of green explicit" philosophy.

## Components

| File | Change |
|------|--------|
| `domain.py` | `class FixRegister(StrEnum)` = `REPAIR, FAIL_FAST, WARN, BETTER_ERROR, DOCUMENT`; add `register: FixRegister = FixRegister.REPAIR` to `FixProposal`. (Shared home so the judgment plan imports it rather than redefining.) |
| `verification/models.py` | add the three new `Verdict` members; include the two `VERIFIED_VIA_*` in `is_green`. |
| `verification/repro.py` | `synthesize_reproducer(..., register=FixRegister.REPAIR)`; for `WARN` emit a `warnings.catch_warnings` assertion, for `BETTER_ERROR` a plain `try/except` asserting the improved error, else today's behavioral/crash assertion (all stdlib — `repro.py` runs as `python repro.py`). The synth prompt is told the register + the failing symbol so the assertion targets it. |
| `verification/verifier.py` | thread the register from `fix.register` into `synthesize_reproducer`; `DOCUMENT` → short-circuit to `UNVERIFIED_DOC_ONLY` before `prepare()`; map the green outcome to the register's verdict label; **overlay base**: only derive when the fixing PR is merged (see below). |
| `integrations/github.py` | `buggy_parent_sha`: return the `merge_commit_sha`'s first parent; **return `None` when there's no merge commit** (unmerged PR) — drop the `base.sha` fallback (it resolves to current `main` = already-fixed, never a valid buggy base). |
| `api/routes/verify.py` | reconstruct `register` from the `fix.generated` payload (default `REPAIR`); pass into `verify()`. |
| `agent/judgment/registers.py` *(future judgment plan)* | imports `FixRegister` from `domain` (does NOT redefine it); adds `REGISTER_GUIDANCE` + `applicable_registers`. (Noted here; not built in this spec.) |

## Merged-to-`main` base (the second goal)

Binding rule: any "base / human-fix ground truth" derivation uses merged PRs only.
- **Overlay (this spec):** the verifier derives the overlay base only when the
  fixing PR is merged. Enforced naturally by `buggy_parent_sha` returning `None`
  for an unmerged PR (no `merge_commit_sha`) → overlay skips → honest `no_repro`.
- **Norm retrieval / register ground-truth (future judgment plan):** when those
  call sites use `discover_pr`, they require `ref.merged` before trusting it.
  Recorded here as the binding rule; enforced when that code is written.

## Register provenance & no-regression

`FixProposal.register` defaults to `REPAIR`. The current single-fix
`generate_fix` path does not set it → `REPAIR` → verify behaves exactly as today.
Only once the judgment plan labels fixes does verify diverge. Verify **trusts**
the label (no independent diff re-classification in this spec).

## Error handling (never crashes)

- Missing/unknown register → `REPAIR` (today's path).
- `WARN`/`BETTER_ERROR` synth failure → degrade as today (`ENVIRONMENTAL_ERROR` /
  the existing repro-synth guard).
- `DOCUMENT` short-circuit emits `UNVERIFIED_DOC_ONLY` and returns cleanly (no
  sandbox).
- Unmerged fixing PR → no overlay, honest `no_repro`. No new crash surface, no
  behavior change for `REPAIR`.

## Observability

`verify.repro_synth` payload gains `register`. New verdict strings flow through
the existing `verify.result` event and the dashboard's `summarize` switch (add
labels for the three new verdicts, mirroring the existing verdict rendering).

## Out of scope (recorded follow-ups)

- The full judgment stage (candidate generation, norm retrieval, selection) — the
  separate agent-judgment plan; this spec only makes verify *ready* for it.
- Independent verify-side re-classification to catch a mislabeled register
  (anti-gaming hardening).
- `BETTER_ERROR` "clarity" is checked structurally (type/message references the
  symbol, differs from baseline) — not via an LLM clarity judge.

## Testing (offline)

- Per-register oracle via scripted repro outputs: `WARN` baseline (no warning →
  reproduces) → rerun (warning fires → `VERIFIED_VIA_WARNING`); `BETTER_ERROR`
  analogous; `REPAIR` unchanged.
- Default-`REPAIR` no-regression: existing verifier tests pass untouched.
- `DOCUMENT` → short-circuits to `UNVERIFIED_DOC_ONLY` with no sandbox run.
- `buggy_parent_sha` returns `None` for an unmerged PR (mock) → overlay skips.
- `register` reconstructed from a persisted run (default `REPAIR` when absent).
- `repro.py` synthesis emits the register-appropriate assertion (string check on
  the generated code for the mock/issue-body path).
- Live metric: once judgment exists, #21062 `WARN` → `VERIFIED_VIA_WARNING`;
  #15743 `REPAIR` → `VERIFIED_VIA_BEHAVIOR` (no over-correction).
