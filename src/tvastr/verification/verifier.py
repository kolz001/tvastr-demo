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
from tvastr.domain import FailurePattern, FileChange, FixProposal, FixRegister, LogEvent, RootCause
from tvastr.events import EventSink, NullEventSink, PipelineEvent
from tvastr.logging import get_logger
from tvastr.verification.models import (
    BEHAVIOR_OK_MARKER,
    Reproducer,
    ReproducerKind,
    RunResult,
    Verdict,
    VerificationResult,
)
from tvastr.verification.regression import discover_scoped_tests, run_scoped_tests
from tvastr.verification.repro import synthesize_reproducer
from tvastr.verification.sandbox import Sandbox, distribution_for_path, installed_module_path

log = get_logger(__name__)


_TAIL_LINES = 60


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _original_exception_seen(result: RunResult, expected: str | None) -> bool:
    if not expected:
        return False
    blob = f"{result.stdout}\n{result.stderr}"
    return f"{expected}:" in blob or f"{expected} " in blob or f"{expected}\n" in blob


_REGISTER_GREEN: dict[FixRegister, tuple[Verdict, str]] = {
    FixRegister.WARN: (Verdict.VERIFIED_VIA_WARNING, "warning"),
    FixRegister.BETTER_ERROR: (Verdict.VERIFIED_VIA_BETTER_ERROR, "better_error"),
}


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
        provision_deps: bool = True,
        source_overlay: bool = True,
    ) -> None:
        self.ctx = ctx
        self.sandbox = sandbox
        self.project_root = project_root  # for scoped-test discovery
        self.sink = event_sink or NullEventSink()
        self.run_id = run_id
        self.provision_deps = provision_deps
        self.source_overlay = source_overlay

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

    def _reproduced(self, result: RunResult, repro: Reproducer) -> bool:
        if repro.kind == ReproducerKind.BEHAVIORAL:
            return BEHAVIOR_OK_MARKER not in result.stdout
        return _original_exception_seen(result, repro.expected_exception) or not result.succeeded

    def _emit_baseline(self, result: RunResult, repro: Reproducer, *, retry: bool = False) -> None:
        self._emit(
            "verify.baseline",
            "verify",
            {
                "exit_code": result.exit_code,
                "stderr_tail": _tail(result.stderr),
                "original_exception_seen": _original_exception_seen(
                    result, repro.expected_exception
                ),
                "timed_out": result.timed_out,
                "retry": retry,
            },
        )

    def _apply_buggy_overlay(
        self, handle: object, pr_number: int, pr_files: list[str]
    ) -> list[FileChange]:
        """Overlay the pre-fix version of the PR's changed files; return FileChanges applied."""
        sha = self.ctx.code_host.buggy_parent_sha(pr_number)
        if not sha:
            return []
        changes: list[FileChange] = []
        for path in pr_files:
            if installed_module_path(path) is None:
                continue  # skip docs/tests/notebooks the PR also touched
            content = self.ctx.code_host.get_file_at_ref(path, sha)
            if content is not None:
                changes.append(
                    FileChange(
                        path=path,
                        patched_content=content,
                        rationale="buggy overlay (pre-fix)",
                    )
                )
        if not changes:
            return []
        handle.apply_changes(changes)  # type: ignore[union-attr]
        self._emit(
            "verify.overlay",
            "verify",
            {"sha": sha, "files": [c.path for c in changes], "ok": True},
        )
        return changes

    def verify(
        self,
        pattern: FailurePattern,
        root_cause: RootCause,
        fix: FixProposal,
        sample_events: list[LogEvent],
        issue_body: str | None,
        pr_number: int | None = None,
        pr_files: list[str] | None = None,
    ) -> VerificationResult:
        started = time.time()
        self._emit(
            "verify.start",
            "verify",
            {"sandbox": self.sandbox.name, "pattern": pattern.fingerprint},
        )

        if fix.register == FixRegister.DOCUMENT:
            # A docs-only fix has no runtime signal — be honest rather than
            # running a behavioral oracle it can never satisfy.
            return self._fail(
                Verdict.UNVERIFIED_DOC_ONLY,
                "none",
                started,
                {"reason": "documentation-only fix; no behavioral oracle applies"},
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
                register=fix.register,
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
             "expected_exception": repro.expected_exception,
             "register": fix.register.value},
        )

        # 2/3. Sandbox + baseline
        handle = self.sandbox.prepare()
        try:
            # 2a. Provision the issue's integration package(s) so the reproducer
            # can locate real source and the patch-applier can resolve the
            # module. Never fatal: a miss degrades to the existing no-repro path.
            if self.provision_deps:
                try:
                    dists = sorted(
                        {
                            d
                            for c in fix.changes
                            if (d := distribution_for_path(c.path)) is not None
                        }
                    )
                    if dists:
                        pr = handle.provision(dists)
                        self._emit(
                            "verify.provision",
                            "verify",
                            {
                                "requested": pr.requested,
                                "installed": pr.installed,
                                "failed": pr.failed,
                                "ok": pr.ok,
                            },
                        )
                except Exception as exc:  # provisioning must never abort verify
                    log.warning("verify.provision.error", error=str(exc))

            handle.write_file("repro.py", repro.code)
            baseline: RunResult = handle.run(["python", "repro.py"], timeout_s=90)
            self._emit_baseline(baseline, repro)
            baseline_reproduced = self._reproduced(baseline, repro)

            overlay_changes: list[FileChange] = []
            if not baseline_reproduced and self.source_overlay and pr_number and pr_files:
                # Released wheel already carries the fix (no_repro). Reconstruct
                # the pre-fix state of the PR's changed files and re-baseline.
                try:
                    overlay_changes = self._apply_buggy_overlay(handle, pr_number, pr_files)
                except Exception as exc:  # overlay must never abort verify
                    overlay_changes = []
                    log.warning("verify.overlay.error", error=str(exc))
                if overlay_changes:
                    baseline = handle.run(["python", "repro.py"], timeout_s=90)
                    self._emit_baseline(baseline, repro, retry=True)
                    baseline_reproduced = self._reproduced(baseline, repro)

            if not baseline_reproduced:
                # Reproducer ran cleanly without ever hitting the bug: we have
                # no signal to evaluate "is the fix necessary?" — be honest.
                return self._finish(
                    handle,
                    Verdict.NO_REPRO,
                    "none",
                    started,
                    {"reason": "baseline run did not reproduce the failure"},
                )
            is_behavioral = repro.kind == ReproducerKind.BEHAVIORAL

            # 4. Apply patch. If we overlaid buggy files, retain the buggy state
            # of any human-PR files the agent's fix does NOT touch, so the rerun
            # tests the agent's fix ALONE against the reproduced bug (the fix wins
            # on any overlapping path). Otherwise each fresh container would revert
            # those files to the already-fixed upstream version → false VERIFIED.
            fix_paths = {c.path for c in fix.changes}
            patch_changes = (
                [oc for oc in overlay_changes if oc.path not in fix_paths]
                + list(fix.changes)
            )
            handle.apply_changes(patch_changes)
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
            if is_behavioral:
                if rerun.exit_code == 0 and BEHAVIOR_OK_MARKER in rerun.stdout:
                    green_verdict, green_oracle = _REGISTER_GREEN.get(
                        fix.register, (Verdict.VERIFIED_VIA_BEHAVIOR, "behavior")
                    )
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
                green_verdict = Verdict.VERIFIED_VIA_REPRODUCER
                green_oracle = "reproducer"

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
                green_verdict,
                green_oracle,
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
