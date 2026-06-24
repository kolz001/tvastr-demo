"""Domain models for the verification loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    """Honest verdicts. `verified_via_*` makes the oracle explicit so a green
    badge in the UI always carries the *kind* of green it is."""

    VERIFIED_VIA_REPRODUCER = "verified_via_reproducer"
    VERIFIED_VIA_SCOPED_TESTS = "verified_via_scoped_tests"
    UNVERIFIED_SMOKE_IMPORT_ONLY = "unverified_smoke_import_only"
    REPRO_BROKEN = "repro_broken"
    NO_REPRO = "no_repro"
    STILL_BROKEN = "still_broken"
    REGRESSION = "regression"
    ENVIRONMENTAL_ERROR = "environmental_error"


class ReproducerSource(StrEnum):
    ISSUE_BODY = "issue_body"
    CLAUDE = "claude"


@dataclass(frozen=True)
class Reproducer:
    source: ReproducerSource
    code: str
    expected_exception: str | None = None  # exception class we're trying to re-trigger


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class VerificationResult:
    verdict: Verdict
    oracle: str  # "reproducer" | "scoped_tests" | "smoke_import" | "none"
    elapsed_s: float
    evidence: dict = field(default_factory=dict)

    @property
    def is_green(self) -> bool:
        return self.verdict in {
            Verdict.VERIFIED_VIA_REPRODUCER,
            Verdict.VERIFIED_VIA_SCOPED_TESTS,
        }
