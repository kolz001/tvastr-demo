"""Verification: recreate the issue, apply the patch, prove the failure is gone.

Closes the loop on "the agent proposed a fix" → "the agent verified the fix."
Sandbox (Docker preferred, subprocess fallback) hosts a hermetic run; a
reproducer is synthesized (from the issue body when possible, via Claude when
not) and executed both before and after the patch. The verdict — and which
oracle produced it — is surfaced honestly through the event stream.
"""

from tvastr.verification.models import (
    Reproducer,
    ReproducerSource,
    RunResult,
    Verdict,
    VerificationResult,
)
from tvastr.verification.repro import synthesize_reproducer
from tvastr.verification.sandbox import (
    DockerSandbox,
    Sandbox,
    SandboxHandle,
    SubprocessSandbox,
    build_sandbox,
)
from tvastr.verification.verifier import Verifier

__all__ = [
    "DockerSandbox",
    "Reproducer",
    "ReproducerSource",
    "RunResult",
    "Sandbox",
    "SandboxHandle",
    "SubprocessSandbox",
    "Verdict",
    "VerificationResult",
    "Verifier",
    "build_sandbox",
    "synthesize_reproducer",
]
