"""Core domain models.

These pydantic models are the contract between layers: ingestion produces
``LogEvent``s, detection groups them into ``FailurePattern``s, the agent turns a
pattern into a ``RootCause`` -> ``FixProposal`` -> ``PullRequestDraft``, and every
escalation is captured as an ``AuditRecord``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Sensitivity(StrEnum):
    """How sensitive a piece of data is — the primary input to LLM routing.

    PUBLIC/INTERNAL data may be sent to a cloud model (Claude); SENSITIVE data
    must be handled locally (Ollama) and redacted before any escalation.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class FixRegister(StrEnum):
    """The *response register* of a fix — how it addresses the failure.

    REPAIR/FAIL_FAST change behavior; WARN/BETTER_ERROR surface it; DOCUMENT only
    informs humans. Verify judges each register by its own success definition.
    """

    REPAIR = "repair"
    FAIL_FAST = "fail_fast"
    WARN = "warn"
    BETTER_ERROR = "better_error"
    DOCUMENT = "document"


class LogEvent(BaseModel):
    """A single log record ingested from a source."""

    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utcnow)
    service: str = "unknown"
    severity: Severity = Severity.ERROR
    message: str
    stack_trace: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    source: str = "simulated"


class FailurePattern(BaseModel):
    """A cluster of similar failures sharing a fingerprint."""

    id: str = Field(default_factory=_new_id)
    fingerprint: str
    title: str
    representative_message: str
    exception_type: str | None = None
    count: int = 0
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    first_seen: datetime = Field(default_factory=_utcnow)
    last_seen: datetime = Field(default_factory=_utcnow)
    sample_event_ids: list[str] = Field(default_factory=list)

    @property
    def is_recurring(self) -> bool:
        return self.count > 1


class RoutingDecision(BaseModel):
    """Where a unit of work was sent and why — recorded for the audit trail."""

    task: str
    target: str  # "local" | "cloud" | "rule"
    model: str
    sensitivity: Sensitivity
    reason: str


class RootCause(BaseModel):
    """The agent's analysis of why a failure pattern occurs."""

    pattern_id: str
    summary: str
    suspected_files: list[str] = Field(default_factory=list)
    confidence: float = 0.0  # 0.0 to 1.0
    reasoning: str = ""


class FileChange(BaseModel):
    """A single file edit within a fix proposal."""

    path: str
    original_snippet: str | None = None
    patched_content: str
    rationale: str = ""
    diff: str | None = None  # unified diff of original → patched, populated when known


class FixProposal(BaseModel):
    """A concrete, reviewable change set that addresses a root cause."""

    pattern_id: str
    summary: str
    changes: list[FileChange] = Field(default_factory=list)
    test_plan: str = ""
    register: FixRegister = FixRegister.REPAIR


class PullRequestDraft(BaseModel):
    """Everything needed to open a PR, before it touches GitHub."""

    pattern_id: str
    title: str
    body: str
    branch: str
    base: str = "main"
    changes: list[FileChange] = Field(default_factory=list)


class PullRequestResult(BaseModel):
    """The outcome of attempting to open a PR."""

    pattern_id: str
    url: str
    number: int | None = None
    branch: str
    created: bool = True
    mocked: bool = False
    dry_run: bool = False


class AuditRecord(BaseModel):
    """An immutable record of one remediation run, persisted to OpenSearch."""

    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utcnow)
    pattern_id: str
    pattern_title: str
    routing: list[RoutingDecision] = Field(default_factory=list)
    root_cause_summary: str | None = None
    pull_request_url: str | None = None
    outcome: str = "pending"  # pending | pr_opened | dry_run | skipped | failed
    notes: str = ""
