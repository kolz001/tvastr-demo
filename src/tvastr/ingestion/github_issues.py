"""Harvest real bug reports from a GitHub repo into ``LogEvent`` JSONL.

Used to build a credible test bed from genuine Haystack issues (or any repo's bug
tracker) instead of synthetic logs. Each issue with a recognizable error signature
in its body becomes one or more ``LogEvent``s — the same shape the simulated
source already replays, so the rest of the pipeline doesn't change.

Mock mode returns a small, deterministic fixture so offline tests and `--use-mocks`
runs work without hitting the GitHub API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tvastr.domain import LogEvent, Severity
from tvastr.logging import get_logger

log = get_logger(__name__)

# An "error-shaped" signature anywhere in the body: "SomeError: details", "SomeException: ...".
# Not anchored to start-of-line — real issues quote tracebacks mid-paragraph.
_ERROR_LINE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_.]*(?:Error|Exception|Warning))\s*:\s*([^\n]+)"
)


@dataclass
class IssueRecord:
    """A minimal, source-agnostic view of a GitHub issue.

    Decoupled from PyGithub so the converter is trivially testable.
    """

    number: int
    title: str
    body: str
    created_at: datetime
    url: str
    labels: list[str]


def _extract_error_signatures(text: str) -> list[tuple[str, str]]:
    """Return (exception_type, message_tail) pairs found anywhere in the body.

    Deduplicates by exception type to avoid one issue producing many near-duplicate
    events when the same traceback is pasted multiple times.
    """
    seen: dict[str, str] = {}
    for match in _ERROR_LINE_RE.finditer(text):
        exc = match.group(1)
        # Strip trailing sentence punctuation / closing fences so the same error
        # quoted with vs without a period fingerprints the same downstream.
        tail = match.group(2).strip().rstrip(".,;`")
        seen.setdefault(exc, tail)
    return list(seen.items())


def _service_from_labels(labels: list[str], default: str) -> str:
    """Use the first ``topic:*`` / ``component:*`` label as the service name, if any."""
    for label in labels:
        for prefix in ("topic:", "component:", "area:"):
            if label.lower().startswith(prefix):
                return label.split(":", 1)[1].strip() or default
    return default


def issue_to_events(issue: IssueRecord, *, default_service: str) -> list[LogEvent]:
    """Convert one issue into zero-or-more ``LogEvent``s.

    An issue with no error-shaped line is skipped (returns ``[]``) — the agent
    needs an exception signature to cluster on, so titles like "feature request:
    add foo" aren't useful here.
    """
    service = _service_from_labels(issue.labels, default_service)
    signatures = _extract_error_signatures(f"{issue.title}\n{issue.body}")
    if not signatures:
        return []

    events: list[LogEvent] = []
    for exc, tail in signatures:
        message = f"{exc}: {tail}"[:400]
        events.append(
            LogEvent(
                timestamp=issue.created_at,
                service=service,
                severity=Severity.ERROR,
                message=message,
                stack_trace=None,
                attributes={
                    "issue_number": str(issue.number),
                    "issue_url": issue.url,
                    "issue_title": issue.title[:160],
                },
                source="github_issues",
            )
        )
    return events


def write_jsonl(events: Iterable[LogEvent], path: Path) -> int:
    """Write events as JSON Lines; return the count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(event.model_dump_json() + "\n")
            written += 1
    return written


class GitHubIssuesFetcher:
    """Live fetcher — pulls bug-labeled issues from a repo via PyGithub.

    ``token`` is optional; without it the GitHub API allows ~60 unauthenticated
    requests/hour, which is fine for a one-shot harvest of a few dozen issues.
    """

    def __init__(self, repo: str, token: str | None = None) -> None:
        self.repo = repo
        self.token = token

    def fetch(self, *, label: str = "bug", limit: int = 50) -> list[IssueRecord]:
        from github import Github  # lazy import — only needed in live mode

        gh = Github(self.token) if self.token else Github()
        repo = gh.get_repo(self.repo)
        log.info("ingest.github_issues.fetch", repo=self.repo, label=label, limit=limit)

        records: list[IssueRecord] = []
        for issue in repo.get_issues(state="all", labels=[label]):
            if issue.pull_request is not None:
                continue  # PRs masquerade as issues in the API
            records.append(
                IssueRecord(
                    number=issue.number,
                    title=issue.title or "",
                    body=issue.body or "",
                    created_at=issue.created_at.replace(tzinfo=UTC)
                    if issue.created_at.tzinfo is None
                    else issue.created_at,
                    url=issue.html_url,
                    labels=[lbl.name for lbl in issue.labels],
                )
            )
            if len(records) >= limit:
                break
        return records


class MockGitHubIssuesFetcher:
    """Deterministic fixture — four issues, two share an exception type."""

    def __init__(self, repo: str = "run-llama/llama_index") -> None:
        self.repo = repo

    def fetch(self, *, label: str = "bug", limit: int = 50) -> list[IssueRecord]:
        log.info("ingest.github_issues.fetch", repo=self.repo, mocked=True)
        base = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        all_records = [
            IssueRecord(
                number=8001,
                title="OpenAI LLM import broken after v0.10 upgrade",
                body=(
                    "Repro:\n"
                    "```\n"
                    "ModuleNotFoundError: No module named 'llama_index.llms.openai'\n"
                    "```\n"
                ),
                created_at=base,
                url=f"https://github.com/{self.repo}/issues/8001",
                labels=["bug", "topic:llm:openai"],
            ),
            IssueRecord(
                number=8002,
                title="Cannot import OpenAI after upgrading to 0.10",
                body=(
                    "Same as #8001. ModuleNotFoundError: No module named "
                    "'llama_index.llms.openai'."
                ),
                created_at=base,
                url=f"https://github.com/{self.repo}/issues/8002",
                labels=["bug", "topic:llm:openai"],
            ),
            IssueRecord(
                number=8010,
                title="Embedding dimension mismatch on Chroma add",
                body=(
                    "ValueError: Embedding dimension 1536 does not match collection "
                    "dimension 768 — happens after switching from text-embedding-3-small "
                    "to a 768-dim model without rebuilding the collection."
                ),
                created_at=base,
                url=f"https://github.com/{self.repo}/issues/8010",
                labels=["bug", "topic:vector_stores"],
            ),
            IssueRecord(
                number=8050,
                title="add support for streaming agent responses",
                body="Would be great to support streaming over agent.chat().",
                created_at=base,
                url=f"https://github.com/{self.repo}/issues/8050",
                labels=["feature-request"],
            ),
        ]
        return all_records[:limit]


def harvest_issues_to_jsonl(
    repo: str,
    out_path: Path,
    *,
    token: str | None = None,
    label: str = "bug",
    limit: int = 50,
    use_mocks: bool = False,
    default_service: str | None = None,
) -> tuple[int, int]:
    """End-to-end: fetch -> convert -> write. Returns (issues_seen, events_written)."""
    fetcher = (
        MockGitHubIssuesFetcher(repo) if use_mocks else GitHubIssuesFetcher(repo, token=token)
    )
    service = default_service or repo.split("/")[-1]
    issues = fetcher.fetch(label=label, limit=limit)

    def _all_events() -> Iterable[LogEvent]:
        for issue in issues:
            yield from issue_to_events(issue, default_service=service)

    # Materialize for the count + write a single pass.
    events = list(_all_events())
    written = write_jsonl(events, out_path)
    log.info(
        "ingest.github_issues.done",
        repo=repo,
        out=str(out_path),
        issues=len(issues),
        events=written,
    )
    return len(issues), written


def load_records_from_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file back into a list of dicts (handy for inspection/tests)."""
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
