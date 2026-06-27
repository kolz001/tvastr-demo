"""Dependencies the agent's nodes and tools operate against.

Defining the integrations as Protocols here (rather than importing concrete
clients) keeps the agent decoupled and trivially testable: any object with the
right shape — real or mock — satisfies the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.events import EventSink, NullEventSink
from tvastr.llm.router import HybridRouter


@runtime_checkable
class CodeHost(Protocol):
    """A source-control host (GitHub) the agent can search, read, and open PRs on."""

    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        """Return candidate file paths matching a query."""
        ...

    def get_file(self, path: str) -> str:
        """Return the contents of a file in the target repo."""
        ...

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        """Return a file's contents at a specific commit/ref, or None."""
        ...

    def commit_before(self, iso_date: str) -> str | None:
        """Return the repo's HEAD commit sha as of a date (ISO 8601), or None."""
        ...

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        """List a directory's entries at a specific commit/ref."""
        ...

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        """Return the commit just before a PR's fix merged (bug present), or None."""
        ...

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        """Create a branch + PR from a draft and return the result."""
        ...


@runtime_checkable
class Notifier(Protocol):
    """A destination for human-facing alerts (Slack)."""

    def notify(self, message: str) -> bool: ...


@dataclass
class AgentContext:
    router: HybridRouter
    code_host: CodeHost
    notifier: Notifier
    min_confidence: float = 0.5
    event_sink: EventSink = field(default_factory=NullEventSink)
    run_id: str | None = None
    doc_grounding: bool = False
