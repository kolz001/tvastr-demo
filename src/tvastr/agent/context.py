"""Dependencies the agent's nodes and tools operate against.

Defining the integrations as Protocols here (rather than importing concrete
clients) keeps the agent decoupled and trivially testable: any object with the
right shape — real or mock — satisfies the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tvastr.domain import PullRequestDraft, PullRequestResult
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
