# 5. Verify-fix loop: recreate, patch, prove the failure is gone

- Status: Accepted
- Date: 2026-05-31

## Context

tvastr's agent produces a `FixProposal` with real `search`/`replace` operations
(see [ADR-0002](0002-hybrid-local-cloud-llm-routing.md) for routing,
[ADR-0004](0004-live-pipeline-instrumentation-via-event-sinks.md) for the
event sink). The dry-run mode renders the unified diff so a human can read it
— but reading a diff isn't the same as knowing the bug is gone. A reviewer
looking at the portfolio project reasonably asks: *did anything actually run?*

"Trust the diff" doesn't scale once we're auto-generating PRs. The loop has
to close: recreate the failure, apply the patch, prove the failure no longer
occurs, and surface the verdict honestly — including the cases where the
verifier can't tell.

## Decision

Add a verification module (`src/tvastr/verification/`) that takes a
`FixProposal` + an issue and runs:

1. **Synthesize a reproducer** — first try to extract a runnable Python block
   from the issue body (cheapest, highest fidelity); fall back to Claude
   synthesis given the exception class + traceback. A `Reproducer` carries
   the source label so the UI can show *where* the repro came from.
2. **Prepare a hermetic sandbox** — Docker preferred, subprocess fallback.
   The `DockerSandbox` runs each command in a one-shot container with
   `--read-only --network=none --cap-drop=ALL --tmpfs=/tmp` so synthesized
   Python can't phone home or write outside `/work`. The `SubprocessSandbox`
   provides a fallback for environments without Docker; isolation is
   weaker (host Python, host filesystem cwd is a tmp dir).
3. **Run the baseline** — execute the reproducer pre-patch. If the original
   exception type does *not* appear in stderr and exit code is 0, the
   reproducer is wrong: verdict `no_repro`, fix recorded as **unverified**.
4. **Apply the patch** — write each `FileChange.patched_content` into the
   sandbox at its path.
5. **Re-run** — execute the reproducer post-patch.
6. **Optionally run scoped regression tests** — if `verify_project_root` is
   configured, discover test files whose name or imports reference the
   changed files, run `pytest -q` on that slice in the sandbox.
7. **Judge** — produce a `Verdict` and `VerificationResult.evidence`. The
   verdict labels are *explicit about which oracle produced them*:

| Verdict | Meaning |
| --- | --- |
| `verified_via_reproducer` | Reproducer no longer raises + exit 0. Strongest. |
| `verified_via_scoped_tests` | Reproducer ambiguous; scoped tests all pass. Weaker. |
| `unverified_smoke_import_only` | Patch applied cleanly; reproducer didn't crash but didn't give a clear signal either. Honest. |
| `no_repro` | Baseline didn't trigger the bug — we can't evaluate the fix. |
| `still_broken` | Reproducer post-patch still raises the original exception. |
| `regression` | Reproducer passes but scoped tests fail. |
| `environmental_error` | Sandbox or repro synthesis failed for a non-verdict reason. |

Every step emits a `verify.*` event through the same `EventSink` the rest of
the pipeline uses, so verification streams live into the UI and persists to
the same `data/runs/<run_id>.jsonl` as the agent events.

## Why these choices

- **Docker over subprocess as the default.** Synthesized Python is a security
  surface — the verifier executes whatever Claude wrote (or whatever was
  pasted into a GitHub issue). Docker's `--network=none --read-only
  --cap-drop=ALL` is significantly stronger than running the same code under
  the host Python. Subprocess remains a fallback rather than a default to
  avoid silently degrading isolation when Docker is available.
- **Issue-body-extraction first, Claude-synth fallback.** The cheapest
  reproducer is one that already exists in the issue. When the body has a
  runnable Python block (parses cleanly, has imports or calls, isn't a
  pasted traceback), we use it. Claude is the fallback when the body has
  only prose, screenshots, or a traceback.
- **"Verified via X" labels instead of plain "verified".** A green badge
  means different things depending on the oracle. Hiding that distinction
  makes the headline ("the agent verifies its own fixes") an overclaim.
  Explicit labels keep the claim honest while still rewarding the cases
  where verification is strong.
- **UI button v1, agent node v2.** Putting verification on the autonomous
  path (between `generate_fix` and `draft_pr`) is the right end state, but
  a verifier whose reliability isn't yet measured shouldn't gate PRs. v1
  ships it as a user-triggered confirmation: the run is dry-run anyway, so
  no PR side-effect is at risk. Once we've watched 50+ verifications, we
  can promote to a graph node.

## Consequences

- **The agent's claim narrows from "proposed a fix" to "proposed a fix and
  here's what 'verified' means in this run."** The README and design doc
  lead with the explicit oracle label, not a green badge alone.
- **Two new operational concerns.** (1) Docker base images need to be built
  and kept in sync with the testbed's dependency surface; we ship
  `verification/Dockerfile.llamaindex` as the LlamaIndex base. (2) The
  reproducer is synthesized code that we then execute — the sandbox's
  security flags are load-bearing, and any future relaxation of them should
  pass through this ADR.
- **Cost.** One additional Claude call per verification (~$0.01–0.05 by
  prompt size) plus ~30s per sandbox run (Docker cold pull is several
  minutes). The UI confirm-modal communicates both before firing.
- **Verifier is the natural home for future signal.** Performance fixes
  could be verified with a benchmark; flaky tests with stress-loops; doc
  fixes via a rendered-output check. The event-sink contract makes each of
  these additive rather than requiring agent changes.
- **Tests get easier, not harder.** The verifier's contract is the event
  sequence + the verdict; both are unit-testable against a `_FakeSandbox`
  and a `_ScriptedLLM`. Real Docker is exercised by smoke tests only.
