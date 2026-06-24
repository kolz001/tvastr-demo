"""The verification loop.

Given a fix proposal, an issue, and an agent context, runs:

    1. synthesize_reproducer  → Reproducer
    2. sandbox.prepare        → SandboxHandle
    3. baseline run           → reproducer pre-patch
    4. apply_changes          → patch the sandbox
    5. rerun                  → reproducer post-patch
    6. triage rerun outcome   → STILL_BROKEN / REPRO_BROKEN / ENVIRONMENTAL_ERROR
    7. optional regression    → scoped tests, only once the reproducer is green
    8. judge                  → Verdict + evidence

Emits ``verify.*`` events through the same ``EventSink`` the rest of the
pipeline uses, so a verification can be streamed live and persisted alongside
the agent's run.
"""

from __future__ import annotations

import time
from pathlib import Path

from tvastr.agent.context import AgentContext
from tvastr.agent.tools.code_retrieval import format_code_for_prompt, retrieve_code_files
from tvastr.domain import FailurePattern, FixProposal, LogEvent, RootCause
from tvastr.events import EventSink, NullEventSink, PipelineEvent
from tvastr.logging import get_logger
from tvastr.verification.models import (
    Reproducer,
    RunResult,
    Verdict,
    VerificationResult,
)
from tvastr.verification.regression import discover_scoped_tests, run_scoped_tests
from tvastr.verification.repro import synthesize_reproducer
from tvastr.verification.sandbox import Sandbox

log = get_logger(__name__)


_TAIL_LINES = 60


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _original_exception_seen(result: RunResult, expected: str | None) -> bool:
    if not expected:
        return False
    blob = f"{result.stdout}\n{result.stderr}"
    return f"{expected}:" in blob or f"{expected} " in blob or f"{expected}\n" in blob


class Verifier:
    """Run one verification end-to-end."""

    def __init__(
        self,
        ctx: AgentContext,
        sandbox: Sandbox,
        *,
        project_root: Path | None = None,
        event_sink: EventSink | None = None,
        run_id: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.sandbox = sandbox
        self.project_root = project_root  # for scoped-test discovery
        self.sink = event_sink or NullEventSink()
        self.run_id = run_id

    def _emit(self, type_: str, step: str, payload: dict | None = None) -> None:
        self.sink.emit(
            PipelineEvent(
                type=type_,  # type: ignore[arg-type]
                layer="output",
                step=step,
                run_id=self.run_id,
                payload=payload or {},
            )
        )

    def verify(
        self,
        pattern: FailurePattern,
        root_cause: RootCause,
        fix: FixProposal,
        sample_events: list[LogEvent],
        issue_body: str | None,
    ) -> VerificationResult:
        started = time.time()
        self._emit(
            "verify.start",
            "verify",
            {"sandbox": self.sandbox.name, "pattern": pattern.fingerprint},
        )

        # 1. Reproducer. Source for the suspected files is supplied lazily —
        # synthesize_reproducer only resolves it on the Claude path, so issues
        # whose body already carries a runnable block skip the retrieval. When
        # Claude does synthesize, seeing the real APIs (constructor params,
        # method names) stops it hallucinating parameters that don't exist.
        def _code_context() -> str:
            try:
                if root_cause.suspected_files:
                    files = retrieve_code_files(
                        self.ctx, root_cause.suspected_files, max_files=2
                    )
                    return format_code_for_prompt(files)
            except Exception as exc:
                log.warning("verify.code_context.failed", error=str(exc))
            return ""

        try:
            repro: Reproducer = synthesize_reproducer(
                pattern,
                root_cause,
                sample_events,
                issue_body,
                self.ctx.router,
                code_context=_code_context,
            )
        except Exception as exc:
            return self._fail(
                Verdict.ENVIRONMENTAL_ERROR,
                "none",
                started,
                {"stage": "repro_synth", "error": str(exc)},
            )
        self._emit(
            "verify.repro_synth",
            "verify",
            {"source": repro.source.value, "code": repro.code,
             "expected_exception": repro.expected_exception},
        )

        # 2/3. Sandbox + baseline
        handle = self.sandbox.prepare()
        try:
            handle.write_file("repro.py", repro.code)
            baseline: RunResult = handle.run(["python", "repro.py"], timeout_s=90)
            saw_original = _original_exception_seen(baseline, repro.expected_exception)
            self._emit(
                "verify.baseline",
                "verify",
                {
                    "exit_code": baseline.exit_code,
                    "stderr_tail": _tail(baseline.stderr),
                    "original_exception_seen": saw_original,
                    "timed_out": baseline.timed_out,
                },
            )
            if not saw_original and baseline.succeeded:
                # Reproducer ran cleanly without ever hitting the bug: we have
                # no signal to evaluate "is the fix necessary?" — be honest.
                return self._finish(
                    handle,
                    Verdict.NO_REPRO,
                    "none",
                    started,
                    {"reason": "baseline run did not reproduce the failure"},
                )

            # 4. Apply patch
            handle.apply_changes(fix.changes)
            self._emit(
                "verify.patch_applied",
                "verify",
                {"files": [c.path for c in fix.changes]},
            )

            # 5. Re-run
            rerun: RunResult = handle.run(["python", "repro.py"], timeout_s=90)
            still_broken = _original_exception_seen(rerun, repro.expected_exception)
            self._emit(
                "verify.rerun",
                "verify",
                {
                    "exit_code": rerun.exit_code,
                    "stderr_tail": _tail(rerun.stderr),
                    "original_exception_seen": still_broken,
                    "timed_out": rerun.timed_out,
                },
            )

            if still_broken:
                return self._finish(
                    handle,
                    Verdict.STILL_BROKEN,
                    "reproducer",
                    started,
                    {"baseline_exit_code": baseline.exit_code, "rerun_exit_code": rerun.exit_code},
                )

            # 6. Triage the rerun outcome honestly — BEFORE spending a scoped
            # test run on a rerun that gave no meaningful fix signal:
            #  - exit 0                       → reproducer is happy, fix works
            #  - timed out                    → environmental
            #  - non-zero exit, no original   → repro itself is broken (Claude
            #    referenced an API that doesn't exist, etc.) — don't pretend
            #    we have a meaningful signal here. Surface as REPRO_BROKEN so
            #    the UI re-arms the verify button instead of looking green-ish.
            if rerun.timed_out:
                return self._finish(
                    handle,
                    Verdict.ENVIRONMENTAL_ERROR,
                    "none",
                    started,
                    {"stage": "rerun", "reason": "timeout"},
                )
            if rerun.exit_code != 0:
                return self._finish(
                    handle,
                    Verdict.REPRO_BROKEN,
                    # The reproducer is exactly what's untrustworthy here, so the
                    # oracle of record is "none" — matching the timeout branch.
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

            # 7. Optional regression check (scoped tests) — only worth running
            # once the reproducer itself is green.
            if self.project_root is not None:
                scoped = discover_scoped_tests(fix.changes, self.project_root)
                if scoped:
                    test_result, counts = run_scoped_tests(handle, scoped)
                    self._emit(
                        "verify.regression",
                        "verify",
                        {"scope": scoped, **counts, "exit_code": test_result.exit_code},
                    )
                    if counts.get("failed", 0) > 0 or counts.get("errors", 0) > 0:
                        return self._finish(
                            handle,
                            Verdict.REGRESSION,
                            "scoped_tests",
                            started,
                            {"scope": scoped, **counts},
                        )

            return self._finish(
                handle,
                Verdict.VERIFIED_VIA_REPRODUCER,
                "reproducer",
                started,
                {"rerun_exit_code": rerun.exit_code},
            )
        finally:
            handle.discard()

    def _finish(
        self,
        handle: object,  # handle is always discarded in the finally block
        verdict: Verdict,
        oracle: str,
        started: float,
        evidence: dict,
    ) -> VerificationResult:
        elapsed = round(time.time() - started, 2)
        result = VerificationResult(
            verdict=verdict, oracle=oracle, elapsed_s=elapsed, evidence=evidence
        )
        self._emit(
            "verify.result",
            "verify",
            {"verdict": verdict.value, "oracle": oracle, "elapsed_s": elapsed, **evidence},
        )
        return result

    def _fail(
        self, verdict: Verdict, oracle: str, started: float, evidence: dict
    ) -> VerificationResult:
        elapsed = round(time.time() - started, 2)
        result = VerificationResult(
            verdict=verdict, oracle=oracle, elapsed_s=elapsed, evidence=evidence
        )
        self._emit(
            "verify.result",
            "verify",
            {"verdict": verdict.value, "oracle": oracle, "elapsed_s": elapsed, **evidence},
        )
        return result
