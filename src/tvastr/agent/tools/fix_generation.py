"""Tool: generate a concrete fix proposal via the cloud model."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.domain import FailurePattern, FileChange, FixProposal, RootCause, RoutingDecision
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You are a senior software engineer. Given a failure and its root cause, propose a "
    "minimal, correct code fix with a short test plan. Be precise and avoid unrelated changes."
)


def generate_fix(
    ctx: AgentContext,
    pattern: FailurePattern,
    root_cause: RootCause,
    code_context: str,
) -> tuple[FixProposal, RoutingDecision]:
    prompt = (
        f"Failure: {pattern.title}\n"
        f"Representative message: {pattern.representative_message}\n"
        f"Root cause: {root_cause.summary}\n"
        f"Suspected files: {', '.join(root_cause.suspected_files) or 'unknown'}\n\n"
        f"Relevant code:\n{code_context or '(not retrieved)'}\n\n"
        "Propose the fix."
    )
    response, decision = ctx.router.run(
        TaskType.FIX_GENERATION, prompt, sensitivity=pattern.sensitivity, system=_SYSTEM
    )

    target_file = root_cause.suspected_files[0] if root_cause.suspected_files else "UNKNOWN.py"
    fix = FixProposal(
        pattern_id=pattern.id,
        summary=response.text,
        changes=[
            FileChange(
                path=target_file,
                patched_content=f"# Proposed fix for: {pattern.title}\n# {response.text}\n",
                rationale=root_cause.summary,
            )
        ],
        test_plan="Add a regression test reproducing the failure; assert it no longer occurs.",
    )
    log.info("tool.fix_generation", pattern=pattern.fingerprint, target=target_file)
    return fix, decision
