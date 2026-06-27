"""A code host that serves reads as of a fixed commit (the issue-era sha).

Wraps an inner code host: ``get_file``/``list_dir`` resolve at ``sha`` (so the
investigator reads the repository as it was when the bug was reported, even for
paths that moved or were deleted on ``main``); everything else delegates.
"""

from __future__ import annotations

from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.logging import get_logger

log = get_logger(__name__)


class IssueEraCodeHost:
    def __init__(self, inner: object, sha: str) -> None:
        self.inner = inner
        self.sha = sha

    def get_file(self, path: str) -> str:
        content = self.inner.get_file_at_ref(path, self.sha)  # type: ignore[attr-defined]
        if content is not None:
            return content
        return self.inner.get_file(path)  # type: ignore[attr-defined]

    def list_dir(self, path: str) -> list[str]:
        return self.inner.list_dir_at_ref(path, self.sha)  # type: ignore[attr-defined]

    # ── everything else delegates to the inner host ──
    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        return self.inner.search_code(query, limit=limit)  # type: ignore[attr-defined]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        return self.inner.get_file_at_ref(path, ref)  # type: ignore[attr-defined]

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        return self.inner.list_dir_at_ref(path, ref)  # type: ignore[attr-defined]

    def commit_before(self, iso_date: str) -> str | None:
        return self.inner.commit_before(iso_date)  # type: ignore[attr-defined]

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        return self.inner.buggy_parent_sha(pr_number)  # type: ignore[attr-defined]

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        return self.inner.open_pull_request(draft)  # type: ignore[attr-defined]
