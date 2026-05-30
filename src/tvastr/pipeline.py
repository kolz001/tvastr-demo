"""End-to-end orchestration: ingest -> detect -> threshold -> agent -> audit.

This is the seam where every layer meets. :func:`build_pipeline` assembles the
whole system from :class:`~tvastr.config.Settings` (mock or real backends), and
:meth:`RemediationPipeline.run` drives a batch of log events through to PRs and an
audit trail.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tvastr.agent import AgentContext, RemediationAgent
from tvastr.config import Settings, get_settings
from tvastr.detection import FailureDetector, ThresholdEngine
from tvastr.domain import AuditRecord, LogEvent
from tvastr.ingestion import LogSource, SimulatedLogSource
from tvastr.ingestion.base import collect
from tvastr.integrations import build_code_host, build_notifier
from tvastr.llm.router import build_router
from tvastr.logging import get_logger
from tvastr.storage import AuditStore, build_audit_store

log = get_logger(__name__)


class PatternOutcome(BaseModel):
    fingerprint: str
    title: str
    count: int
    sensitivity: str
    outcome: str
    pull_request_url: str | None = None
    routing: list[dict] = Field(default_factory=list)


class PipelineRun(BaseModel):
    events_ingested: int
    patterns_detected: int
    patterns_selected: int
    outcomes: list[PatternOutcome] = Field(default_factory=list)


class RemediationPipeline:
    def __init__(
        self,
        *,
        detector: FailureDetector,
        threshold: ThresholdEngine,
        agent: RemediationAgent,
        audit_store: AuditStore,
        log_source: LogSource,
    ) -> None:
        self.detector = detector
        self.threshold = threshold
        self.agent = agent
        self.audit_store = audit_store
        self.log_source = log_source

    def run(self, events: list[LogEvent] | None = None) -> PipelineRun:
        if events is None:
            events = collect(self.log_source)
        events_by_id = {e.id: e for e in events}

        patterns = self.detector.detect(events)
        selected = self.threshold.select(patterns)

        outcomes: list[PatternOutcome] = []
        for pattern in selected:
            sample_events = [
                events_by_id[eid] for eid in pattern.sample_event_ids if eid in events_by_id
            ]
            final = self.agent.run({"pattern": pattern, "sample_events": sample_events})
            self.threshold.mark_handled(pattern)

            pr_result = final.get("pr_result")
            routing = final.get("routing", [])
            record = AuditRecord(
                pattern_id=pattern.id,
                pattern_title=pattern.title,
                routing=routing,
                root_cause_summary=(rc := final.get("root_cause")) and rc.summary,
                pull_request_url=pr_result.url if pr_result else None,
                outcome=final.get("outcome", "skipped"),
                notes=final.get("notes", ""),
            )
            self.audit_store.save(record)

            outcomes.append(
                PatternOutcome(
                    fingerprint=pattern.fingerprint,
                    title=pattern.title,
                    count=pattern.count,
                    sensitivity=pattern.sensitivity.value,
                    outcome=record.outcome,
                    pull_request_url=record.pull_request_url,
                    routing=[d.model_dump(mode="json") for d in routing],
                )
            )

        run = PipelineRun(
            events_ingested=len(events),
            patterns_detected=len(patterns),
            patterns_selected=len(selected),
            outcomes=outcomes,
        )
        log.info(
            "pipeline.run.complete",
            ingested=run.events_ingested,
            detected=run.patterns_detected,
            selected=run.patterns_selected,
        )
        return run


def build_pipeline(settings: Settings | None = None) -> RemediationPipeline:
    settings = settings or get_settings()
    router = build_router(settings)
    ctx = AgentContext(
        router=router,
        code_host=build_code_host(settings),
        notifier=build_notifier(settings),
    )
    return RemediationPipeline(
        detector=FailureDetector(),
        threshold=ThresholdEngine(
            recurrence_threshold=settings.recurrence_threshold,
            dedup_window_minutes=settings.dedup_window_minutes,
        ),
        agent=RemediationAgent(ctx),
        audit_store=build_audit_store(settings),
        log_source=SimulatedLogSource(),
    )
