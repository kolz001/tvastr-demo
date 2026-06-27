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
from tvastr.events import EventSink, NullEventSink, PipelineEvent
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
    pr_title: str | None = None
    pr_branch: str | None = None
    pr_changes: list[dict] = Field(default_factory=list)
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
        event_sink: EventSink | None = None,
        run_id: str | None = None,
    ) -> None:
        self.detector = detector
        self.threshold = threshold
        self.agent = agent
        self.audit_store = audit_store
        self.log_source = log_source
        self.event_sink: EventSink = event_sink or NullEventSink()
        self.run_id = run_id

    def _emit(self, type_: str, step: str, layer: str = "ingestion", **payload: object) -> None:
        self.event_sink.emit(
            PipelineEvent(
                type=type_,  # type: ignore[arg-type]
                layer=layer,  # type: ignore[arg-type]
                step=step,
                run_id=self.run_id,
                payload=dict(payload),
            )
        )

    def _issue_era_host(self, base_host: object, sample_events: list) -> object:
        """Return an IssueEraCodeHost if the flag is on and a sha resolves; else base_host."""
        if not getattr(self.agent.ctx, "issue_era_retrieval", False) or not sample_events:
            return base_host
        try:
            iso = sample_events[0].timestamp.isoformat()
            sha = base_host.commit_before(iso)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("retrieval.issue_era.resolve_failed", error=str(exc))
            sha = None
        self._emit(
            "retrieval.issue_era",
            "agent",
            layer="agent",
            sha=sha,
            ok=bool(sha),
        )
        if not sha:
            return base_host
        from tvastr.integrations.issue_era_host import IssueEraCodeHost

        return IssueEraCodeHost(base_host, sha)

    def run(
        self,
        events: list[LogEvent] | None = None,
        *,
        run_meta: dict | None = None,
        pr_ref: object | None = None,
        pr_diff: object | None = None,
        issue_body: str | None = None,
    ) -> PipelineRun:
        self._emit("pipeline.start", "pipeline", **(run_meta or {}))
        if events is None:
            events = collect(self.log_source)
        events_by_id = {e.id: e for e in events}
        self._emit(
            "ingest.read",
            "ingest",
            count=len(events),
            services=sorted({e.service for e in events}),
        )

        patterns = self.detector.detect(events)
        self._emit(
            "detect.cluster",
            "cluster",
            layer="detection",
            patterns=len(patterns),
            breakdown=[
                {"title": p.title, "count": p.count, "sensitivity": p.sensitivity.value}
                for p in patterns
            ],
        )
        sensitive = [p for p in patterns if p.sensitivity.value == "sensitive"]
        if sensitive:
            self._emit(
                "detect.pii",
                "pii_scan",
                layer="detection",
                count=len(sensitive),
                patterns=[p.title for p in sensitive],
            )

        selected = self.threshold.select(patterns)
        self._emit(
            "threshold.select",
            "threshold",
            layer="detection",
            recurrence_threshold=self.threshold.recurrence_threshold,
            selected=[p.title for p in selected],
        )

        base_code_host = self.agent.ctx.code_host
        outcomes: list[PatternOutcome] = []
        for pattern in selected:
            sample_events = [
                events_by_id[eid] for eid in pattern.sample_event_ids if eid in events_by_id
            ]
            # Issue-era retrieval: serve the agent's reads as of the issue date.
            self.agent.ctx.code_host = self._issue_era_host(base_code_host, sample_events)
            self._emit(
                "agent.start",
                "agent",
                layer="agent",
                pattern=pattern.fingerprint,
                title=pattern.title,
            )
            final = self.agent.run(
                {
                    "pattern": pattern,
                    "sample_events": sample_events,
                    "pr_ref": pr_ref,
                    "pr_diff": pr_diff,
                    "issue_body": issue_body,
                }
            )
            self.threshold.mark_handled(pattern)

            pr_result = final.get("pr_result")
            pr_draft = final.get("pr_draft")
            routing = final.get("routing", [])
            outcome_label = final.get("outcome", "skipped")
            # In dry-run, the PR URL is a sentinel — don't pollute the audit log with it.
            audit_pr_url = pr_result.url if pr_result and not pr_result.dry_run else None
            record = AuditRecord(
                pattern_id=pattern.id,
                pattern_title=pattern.title,
                routing=routing,
                root_cause_summary=(rc := final.get("root_cause")) and rc.summary,
                pull_request_url=audit_pr_url,
                outcome=outcome_label,
                notes=final.get("notes", ""),
            )
            self.audit_store.save(record)
            self._emit(
                "audit.saved",
                "audit",
                layer="output",
                outcome=outcome_label,
                pattern=pattern.fingerprint,
                pull_request_url=audit_pr_url,
            )

            outcomes.append(
                PatternOutcome(
                    fingerprint=pattern.fingerprint,
                    title=pattern.title,
                    count=pattern.count,
                    sensitivity=pattern.sensitivity.value,
                    outcome=record.outcome,
                    pull_request_url=audit_pr_url,
                    pr_title=pr_draft.title if pr_draft else None,
                    pr_branch=pr_draft.branch if pr_draft else None,
                    pr_changes=[
                        {"path": c.path, "rationale": c.rationale, "diff": c.diff or ""}
                        for c in (pr_draft.changes if pr_draft else [])
                    ],
                    routing=[d.model_dump(mode="json") for d in routing],
                )
            )

        # Restore the unwrapped host so a second run() on this pipeline re-wraps
        # from the real base, never IssueEraCodeHost(IssueEraCodeHost(...)).
        self.agent.ctx.code_host = base_code_host

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
        self._emit(
            "pipeline.end",
            "pipeline",
            ingested=run.events_ingested,
            detected=run.patterns_detected,
            selected=run.patterns_selected,
            outcome=(outcomes[0].outcome if outcomes else "no_patterns_selected"),
        )
        return run


def build_pipeline(
    settings: Settings | None = None,
    *,
    log_source: LogSource | None = None,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
) -> RemediationPipeline:
    settings = settings or get_settings()
    sink = event_sink or NullEventSink()
    router = build_router(settings)
    # Thread the sink + run_id into the router so its decisions and LLM calls emit too.
    router.event_sink = sink
    router.run_id = run_id
    ctx = AgentContext(
        router=router,
        code_host=build_code_host(settings),
        notifier=build_notifier(settings),
        event_sink=sink,
        run_id=run_id,
        doc_grounding=settings.doc_grounding
        and not settings.use_mocks
        and bool(settings.anthropic_api_key),
        issue_era_retrieval=settings.issue_era_retrieval,
    )
    return RemediationPipeline(
        detector=FailureDetector(),
        threshold=ThresholdEngine(
            recurrence_threshold=settings.recurrence_threshold,
            dedup_window_minutes=settings.dedup_window_minutes,
        ),
        agent=RemediationAgent(ctx),
        audit_store=build_audit_store(settings),
        log_source=log_source or SimulatedLogSource(),
        event_sink=sink,
        run_id=run_id,
    )
