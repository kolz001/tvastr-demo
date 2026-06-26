"""GitHub integration — satisfies the :class:`~tvastr.agent.context.CodeHost` protocol.

The mock returns deterministic, plausible data so the agent runs offline. The real
client uses PyGithub to search code, read files, and open a branch + PR from a draft.
:class:`DryRunCodeHost` is a thin decorator that lets reads pass through to the
underlying host but suppresses the PR-creation side effect.
"""

from __future__ import annotations

from typing import Protocol

from tvastr.config import Settings
from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.logging import get_logger

log = get_logger(__name__)


class _CodeHostLike(Protocol):
    def search_code(self, query: str, *, limit: int = 5) -> list[str]: ...
    def get_file(self, path: str) -> str: ...
    def get_file_at_ref(self, path: str, ref: str) -> str | None: ...
    def buggy_parent_sha(self, pr_number: int) -> str | None: ...
    def list_dir(self, path: str) -> list[str]: ...
    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult: ...


class MockGitHubClient:
    """Offline stand-in for GitHub. Records the would-be PR instead of opening one."""

    def __init__(self, repo: str = "run-llama/llama_index", base_branch: str = "main") -> None:
        self.repo = repo
        self.base_branch = base_branch
        self.opened_prs: list[PullRequestDraft] = []

    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        log.info("github.search_code", repo=self.repo, query=query, mocked=True)
        # Deterministic, repo-plausible guesses keyed off the query. Mirrors the
        # post-v0.10 namespace layout: core + per-integration subpackages.
        stem = query.lower().replace("error", "").replace("exception", "").strip() or "module"
        return [f"llama_index/core/{stem}.py", f"llama_index/llms/openai/{stem}.py"][:limit]

    def get_file(self, path: str) -> str:
        log.info("github.get_file", repo=self.repo, path=path, mocked=True)
        return (
            f"# (mock) contents of {path} from {self.repo}\n"
            "def run(self, *args, **kwargs):\n"
            "    ...  # implementation elided in mock mode\n"
        )

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        log.info("github.get_file_at_ref", repo=self.repo, path=path, ref=ref, mocked=True)
        return (
            f"# (mock) contents of {path} @ {ref} from {self.repo}\n"
            "def run(self, *args, **kwargs):\n"
            "    ...  # implementation elided in mock mode\n"
        )

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        log.info("github.buggy_parent_sha", repo=self.repo, pr=pr_number, mocked=True)
        return f"buggyparent{pr_number}"

    def list_dir(self, path: str) -> list[str]:
        log.info("github.list_dir", repo=self.repo, path=path, mocked=True)
        base = path.rstrip("/")
        return [f"{base}/base.py", f"{base}/utils.py"]

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        self.opened_prs.append(draft)
        number = len(self.opened_prs)
        url = f"https://github.com/{self.repo}/pull/{900000 + number}"
        log.info("github.open_pr", repo=self.repo, branch=draft.branch, url=url, mocked=True)
        return PullRequestResult(
            pattern_id=draft.pattern_id,
            url=url,
            number=number,
            branch=draft.branch,
            created=True,
            mocked=True,
        )


class GitHubClient:
    """Real GitHub client (PyGithub). Used when ``use_mocks`` is false."""

    def __init__(self, token: str, repo: str, base_branch: str = "main") -> None:
        self.token = token
        self.repo_name = repo
        self.base_branch = base_branch
        self._repo = None

    def _get_repo(self):
        if self._repo is None:
            from github import Github  # lazy import

            self._repo = Github(self.token).get_repo(self.repo_name)
        return self._repo

    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        from github import Github  # lazy import
        from github.GithubException import GithubException

        gh = Github(self.token)
        paths: list[str] = []
        try:
            results = gh.search_code(f"{query} repo:{self.repo_name}")
            # Iterate directly rather than slicing: PaginatedList's index-based
            # slice (results[:limit]) can overrun the materialised page when the
            # search API's reported count is optimistic, raising IndexError. Its
            # __iter__ only yields actually-fetched elements, so it is safe.
            for item in results:
                if len(paths) >= limit:
                    break
                paths.append(item.path)
        except (GithubException, IndexError) as exc:
            # A search failure (rate limit, 422 on a free-text query) or the
            # pagination quirk above must not crash the run — degrade to "no
            # suspected files" so the agent escalates to a human via the
            # confidence gate instead.
            log.warning(
                "github.search_code.failed",
                repo=self.repo_name,
                query=query,
                error=str(exc),
            )
            return []
        log.info("github.search_code", repo=self.repo_name, query=query, hits=len(paths))
        return paths

    def get_file(self, path: str) -> str:
        repo = self._get_repo()
        contents = repo.get_contents(path, ref=self.base_branch)
        return contents.decoded_content.decode("utf-8")

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        try:
            contents = self._get_repo().get_contents(path, ref=ref)
        except Exception as exc:
            log.warning("github.get_file_at_ref.failed", path=path, ref=ref, error=str(exc))
            return None
        if isinstance(contents, list):  # a directory, not a file
            return None
        return contents.decoded_content.decode("utf-8")

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        try:
            repo = self._get_repo()
            pr = repo.get_pull(pr_number)
            merge_sha = pr.merge_commit_sha
            if merge_sha:
                commit = repo.get_commit(merge_sha)
                if commit.parents:
                    return str(commit.parents[0].sha)
            base_sha = getattr(getattr(pr, "base", None), "sha", None)
            return str(base_sha) if base_sha else None
        except Exception as exc:
            log.warning("github.buggy_parent_sha.failed", pr=pr_number, error=str(exc))
            return None

    def list_dir(self, path: str) -> list[str]:
        try:
            contents = self._get_repo().get_contents(path, ref=self.base_branch)
        except Exception as exc:
            log.warning("github.list_dir.failed", path=path, error=str(exc))
            return []
        items = contents if isinstance(contents, list) else [contents]
        return [c.path for c in items]

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        repo = self._get_repo()
        base = repo.get_branch(self.base_branch)
        repo.create_git_ref(ref=f"refs/heads/{draft.branch}", sha=base.commit.sha)

        for change in draft.changes:
            try:
                existing = repo.get_contents(change.path, ref=draft.branch)
                repo.update_file(
                    change.path,
                    f"tvastr: {draft.title}",
                    change.patched_content,
                    existing.sha,
                    branch=draft.branch,
                )
            except Exception:
                repo.create_file(
                    change.path,
                    f"tvastr: {draft.title}",
                    change.patched_content,
                    branch=draft.branch,
                )

        pr = repo.create_pull(
            title=draft.title, body=draft.body, head=draft.branch, base=draft.base
        )
        log.info("github.open_pr", repo=self.repo_name, number=pr.number, url=pr.html_url)
        return PullRequestResult(
            pattern_id=draft.pattern_id,
            url=pr.html_url,
            number=pr.number,
            branch=draft.branch,
            created=True,
        )


class DryRunCodeHost:
    """Decorator that lets the agent investigate against ``inner`` but never opens a PR.

    Reads (``search_code`` / ``get_file``) pass through unchanged — the value of
    dry-run is exercising the real read path. Only ``open_pull_request`` is
    intercepted: the draft is captured for inspection and a non-created
    :class:`PullRequestResult` is returned so downstream nodes can branch on it.
    """

    def __init__(self, inner: _CodeHostLike, repo: str) -> None:
        self.inner = inner
        self.repo = repo
        self.captured_drafts: list[PullRequestDraft] = []

    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        return self.inner.search_code(query, limit=limit)

    def get_file(self, path: str) -> str:
        return self.inner.get_file(path)

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        return self.inner.get_file_at_ref(path, ref)

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        return self.inner.buggy_parent_sha(pr_number)

    def list_dir(self, path: str) -> list[str]:
        return self.inner.list_dir(path)

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        self.captured_drafts.append(draft)
        log.info(
            "github.dry_run.capture",
            repo=self.repo,
            branch=draft.branch,
            files=[c.path for c in draft.changes],
        )
        return PullRequestResult(
            pattern_id=draft.pattern_id,
            url=f"dry-run://{self.repo}/{draft.branch}",
            number=None,
            branch=draft.branch,
            created=False,
            dry_run=True,
        )


def build_code_host(
    settings: Settings,
) -> MockGitHubClient | GitHubClient | DryRunCodeHost:
    if settings.use_mocks or not settings.github_token:
        if not settings.use_mocks:
            log.warning("github.no_token", msg="falling back to mock GitHub client")
        inner: _CodeHostLike = MockGitHubClient(settings.github_repo, settings.github_base_branch)
    else:
        inner = GitHubClient(
            settings.github_token, settings.github_repo, settings.github_base_branch
        )
    if settings.dry_run:
        log.info("github.dry_run.enabled", repo=settings.github_repo)
        return DryRunCodeHost(inner, repo=settings.github_repo)
    return inner  # type: ignore[return-value]
