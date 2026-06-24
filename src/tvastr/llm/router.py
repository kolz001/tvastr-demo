"""Hybrid LLM router.

Chooses between the local model (Ollama) and the cloud model (Claude) per task,
following the project's privacy-first routing strategy:

    task                         target   rationale
    ---------------------------  -------  ------------------------------------------
    log parsing / clustering     local    sensitive data, no external calls
    failure summarization        local    PII redaction happens here before escalation
    root cause reasoning         cloud    complex multi-step thinking
    code fix generation          cloud    high accuracy needed
    pr description writing       cloud    natural language quality

Sensitive content is always redacted (:mod:`tvastr.pii`) before it is handed to the
cloud backend, so raw sensitive data never crosses the local boundary.
"""

from __future__ import annotations

import time
from enum import StrEnum

from tvastr.config import Settings
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.events import EventSink, NullEventSink, PipelineEvent
from tvastr.llm.base import LLMClient, LLMResponse
from tvastr.llm.claude import ClaudeClient, MockClaudeClient
from tvastr.llm.local import MockOllamaClient, OllamaClient
from tvastr.logging import get_logger
from tvastr.pii import contains_pii, redact

log = get_logger(__name__)


class TaskType(StrEnum):
    LOG_PARSING = "log_parsing"
    SUMMARIZATION = "summarization"
    ROOT_CAUSE = "root_cause"
    FIX_GENERATION = "fix_generation"
    PR_DESCRIPTION = "pr_description"
    PR_ANALYSIS = "pr_analysis"
    FIX_COMPARISON = "fix_comparison"


_LOCAL_TASKS = {TaskType.LOG_PARSING, TaskType.SUMMARIZATION}


class HybridRouter:
    """Routes a task+payload to the appropriate backend and records the decision."""

    def __init__(
        self,
        local: LLMClient,
        cloud: LLMClient,
        *,
        event_sink: EventSink | None = None,
        run_id: str | None = None,
    ) -> None:
        self.local = local
        self.cloud = cloud
        self.event_sink: EventSink = event_sink or NullEventSink()
        self.run_id = run_id

    def route_target(self, task: TaskType, sensitivity: Sensitivity) -> str:
        """Decide ``"local"`` vs ``"cloud"`` for a task at a sensitivity level."""
        if task in _LOCAL_TASKS:
            return "local"
        # Cloud-eligible tasks: still cloud, but the payload will be redacted first.
        return "cloud"

    def run(
        self,
        task: TaskType,
        prompt: str,
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        system: str | None = None,
    ) -> tuple[LLMResponse, RoutingDecision]:
        target = self.route_target(task, sensitivity)
        client = self.local if target == "local" else self.cloud

        payload = prompt
        reason: str
        redacted_fields: list[str] = []
        if target == "local":
            reason = "kept local — sensitive data must not leave the boundary"
        else:
            redacted, found = redact(prompt)
            payload = redacted
            redacted_fields = list(found)
            if found:
                reason = f"escalated to cloud after redacting {', '.join(found)}"
            elif sensitivity is Sensitivity.SENSITIVE:
                reason = "escalated to cloud (flagged sensitive, no raw PII matched)"
            else:
                reason = "escalated to cloud — complex reasoning, no sensitive data"

        decision = RoutingDecision(
            task=task.value,
            target=target,
            model=client.model,
            sensitivity=sensitivity,
            reason=reason,
        )
        log.info(
            "router.route",
            task=task.value,
            target=target,
            model=client.model,
            sensitivity=sensitivity.value,
        )
        self.event_sink.emit(
            PipelineEvent(
                type="router.decide",
                layer="agent",
                step=task.value,
                run_id=self.run_id,
                payload={
                    "task": task.value,
                    "target": target,
                    "model": client.model,
                    "sensitivity": sensitivity.value,
                    "reason": reason,
                    "redacted_fields": redacted_fields,
                },
            )
        )
        started = time.perf_counter()
        response = client.complete(payload, system=system)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.event_sink.emit(
            PipelineEvent(
                type="llm.call",
                layer="agent",
                step=task.value,
                run_id=self.run_id,
                payload={
                    "task": task.value,
                    "target": target,
                    "model": client.model,
                    "elapsed_ms": elapsed_ms,
                    "mocked": getattr(response, "mocked", False),
                    "prompt": payload,
                    "system": system,
                    "response": response.text,
                },
            )
        )
        return response, decision


def build_router(settings: Settings) -> HybridRouter:
    """Construct a router with mock or real backends based on ``settings``."""
    if settings.use_mocks:
        return HybridRouter(
            local=MockOllamaClient(settings.ollama_model),
            cloud=MockClaudeClient(settings.claude_model),
        )

    cloud: LLMClient
    if settings.anthropic_api_key:
        cloud = ClaudeClient(settings.anthropic_api_key, settings.claude_model)
    else:
        log.warning("router.no_anthropic_key", msg="falling back to mock Claude client")
        cloud = MockClaudeClient(settings.claude_model)

    return HybridRouter(
        local=OllamaClient(settings.ollama_base_url, settings.ollama_model),
        cloud=cloud,
    )


__all__ = ["HybridRouter", "TaskType", "build_router", "contains_pii"]
