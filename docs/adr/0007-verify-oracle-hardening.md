# 7. Verify oracle hardening

- Status: Accepted
- Date: 2026-07-02

This ADR **extends, and does not supersede,** [ADR-0005](0005-verify-fix-loop.md).
The original decision — hermetic sandbox, synthesize a reproducer, baseline →
patch → re-run, explicit oracle-labeled verdicts — still stands. Everything
below is what running that loop against real, closed LlamaIndex issues forced
it to grow.

## Context

ADR-0005's loop assumed a reasonably favorable world: the reproducer would
run in the base sandbox image, the bug would still be present in whatever
source the sandbox saw, the patch would land where the reproducer's imports
actually resolved, and a reproducer that failed to execute was itself a
verdict (`environmental_error`) rather than something to fix and retry. Real
issues broke each of these assumptions in turn:

- **Long-tail dependencies.** LlamaIndex is hundreds of separately
  installable integration packages; the base sandbox image ships a fixed
  handful. An issue on the Postgres vector store or a niche embeddings
  provider failed before the reproducer ever got to exercise the bug — an
  honest verdict, but an uninformative one.
- **Released-wheel-already-fixed.** The sandbox's Python environment
  installs released wheels. For a closed issue with a merged PR, the release
  containing that fix is often already out — so the baseline run doesn't
  reproduce anything, and the verdict is a truthful but useless `no_repro`.
- **The patch didn't land on the import path the reproducer actually used.**
  The reproducer imports the pip-installed package; early patch application
  wrote to a repo-relative path that nothing imported, so a "passing"
  reproducer wasn't exercising the agent's fix at all.
- **Behavioral bugs need behavioral oracles, and not every correct fix
  changes behavior.** A reproducer that only checks "did the crash stop"
  passes for a fix that suppresses the exception without fixing the
  underlying value — a real, LLM-authored failure mode. And a
  maintainer-correct fix whose whole job is to *warn* instead of silently
  misbehaving (or to raise a *clearer* error instead of a cryptic one) will
  never look "fixed" to an oracle that only knows how to check for absence of
  the original exception.
- **A broken reproducer wasn't worth a retry.** Real dependency and import
  errors surfaced by early attempts, distinct from the bug under test, were
  simply recorded as a verdict rather than treated as fixable state.

## Decision

Layer four capabilities onto the ADR-0005 loop, in the order the verifier
actually runs them:

1. **Dependency provisioning.** Before running anything, derive the
   distribution(s) implied by the fix's changed files and `pip install
   --target` them into an isolated directory prepended onto `PYTHONPATH`
   inside the sandbox. Gated by `TVASTR_VERIFY_PROVISION_DEPS` (default on).
2. **Behavioral reproducers with a positive postcondition, hardened by
   self-critique.** Reproducers are tagged `behavioral` (assert the *correct*
   value comes back) or `crash` (assert the original exception is gone).
   Behavioral repros for `REPAIR`/`FAIL_FAST` fixes get a second adversarial
   pass (`REPRO_CRITIQUE`): "would a suppress-only fix still pass this
   assertion?" — if yes, rewrite toward a real check.
3. **Buggy-file overlay on `no_repro`.** When a linked PR is known and the
   baseline doesn't reproduce, overlay the PR-touched files at their
   pre-merge (buggy-parent) commit, then retry the baseline once. Files the
   agent's own fix doesn't touch stay in their overlaid buggy state through
   the post-patch rerun too, so the rerun measures the agent's fix alone —
   not a mixture of the agent's fix and an already-fixed upstream file.
4. **Register-aware oracles.** `FixProposal.register` (`REPAIR`, `FAIL_FAST`,
   `WARN`, `BETTER_ERROR`, `DOCUMENT`) tells the verifier what kind of
   success to look for. `DOCUMENT` short-circuits to `unverified_doc_only`
   with no sandbox. `WARN`/`BETTER_ERROR` map to their own oracles
   (`verified_via_warning`, `verified_via_better_error`) and skip the
   round-trip-biased critique from (2), since their assertions are
   intentionally non-behavioral. `REPAIR`/`FAIL_FAST` fall through to the
   default behavioral oracle. Verify **trusts** the register `generate_fix`
   attached — it does not independently re-classify the fix. This is a named,
   accepted gap: a mislabeled register is graded by the wrong oracle today.
5. **Patch application on the actual import path.** The patch is staged and
   bootstrapped inside the *same* container, immediately before the
   reproducer's import resolves (`sh -c "python apply.py && <repro>"`) — the
   one place `--read-only` is deliberately relaxed, scoped to that single
   write. All other sandbox hardening (`--network=none`, `--cap-drop=ALL`,
   `--rm`) is unchanged.
6. **Bounded reproducer repair loop.** On a `repro_broken` verdict (the
   reproducer itself failed to execute, for reasons unrelated to the bug
   under test), repair it using the real error surfaced and retry — up to
   `_MAX_REPRO_REPAIR = 2` times (3 attempts total). **Each attempt gets a
   fresh sandbox handle, prepared and provisioned from scratch.** An earlier
   version prepared and provisioned one handle outside the loop and reused it
   across repair attempts; attempt 2's baseline then ran against a handle
   still carrying attempt 1's applied patch — a silent, cross-attempt state
   leak that could produce a false verdict on the *second* attempt, not the
   first. The fix moves `prepare()`/`provision()` inside the loop body with
   `discard()` in a per-iteration `finally`, and a regression test asserts
   two distinct handle objects across two attempts.

The verdict enum grows to reflect all of this honestly: `verified_via_behavior`,
`verified_via_warning`, `verified_via_better_error`, `masks_symptom`,
`repro_broken`, and `unverified_doc_only` join the original seven from
ADR-0005 — thirteen verdicts in total, each with a distinct meaning rather
than a shared "verified" or "failed" bucket.

## Why these choices

- **Provisioning over a bigger base image.** A fixed base image can never
  keep pace with hundreds of integration packages; installing on demand,
  scoped to what the specific fix touches, is the only version of this that
  scales with the testbed's own namespace split.
- **Overlay only on `no_repro`, only for PR-known issues, lazily.** This
  keeps the common case (bug still reproduces against the released wheel)
  untouched — the overlay is a targeted answer to a specific, diagnosable
  failure mode, not a default behavior change.
- **Register-aware, not register-blind — but not register-verified either.**
  Making verify polymorphic on the register is a small, contained change (a
  lookup table plus one short-circuit) compared to making `generate_fix`'s
  register choice independently auditable, which is future work explicitly
  deferred rather than silently assumed away.
- **Fresh sandbox per repair attempt, even though it's more expensive.**
  Reusing a handle across attempts is the more efficient implementation and
  the one that was shipped first — and it was wrong. State bleeding across
  attempts is a correctness bug, not a performance one; the fix trades a bit
  of wall-clock time for attempts that are actually independent.

## Consequences

- **The "honest verdict" contract from ADR-0005 gets harder to keep, not
  easier, as the taxonomy grows.** Thirteen verdicts is more for a reviewer
  to learn than seven; the UI's plain-English glosses (`VERDICT_GLOSS`) exist
  specifically so nobody has to memorize the enum to understand a run.
- **Verify's trust boundary with `generate_fix` is now explicit and
  load-bearing.** Register-aware grading only works if the register is
  honest; anti-gaming hardening (verify-side re-classification) is recorded
  as deferred, not solved.
- **The dependency-provisioning and overlay steps both widen what the
  sandbox does before the security-relevant part (running Claude-authored
  Python) happens.** `--network=none`/`--read-only`/`--cap-drop=ALL` remain
  the load-bearing controls on the reproducer execution itself; provisioning
  and overlay both run as their own scoped, non-`--read-only` steps ahead of
  it, which is a wider trusted-setup surface than ADR-0005 originally
  described.
- **Tests get more valuable, not just more numerous.** The cross-attempt
  patch leak and the register/oracle mapping are exactly the kind of subtle
  state-machine bugs unit tests catch better than manual verification —
  both landed with dedicated regression tests.
