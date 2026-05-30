"""GitHub integration — satisfies the :class:`~tvastr.agent.context.CodeHost` protocol.

The mock returns deterministic, plausible data so the agent runs offline. The real
client uses PyGithub to search code, read files, and open a branch + PR from a draft.
"""

from __future__ import annotations

from tvastr.config import Settings
from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.logging import get_logger

log = get_logger(__name__)


class MockGitHubClient:
    """Offline stand-in for GitHub. Records the would-be PR instead of opening one."""

    def __init__(self, repo: str = "deepset-ai/haystack", base_branch: str = "main") -> None:
        self.repo = repo
        self.base_branch = base_branch
        self.opened_prs: list[PullRequestDraft] = []

    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        log.info("github.search_code", repo=self.repo, query=query, mocked=True)
        # Deterministic, repo-plausible guesses keyed off the query.
        stem = query.lower().replace("error", "").replace("exception", "").strip() or "module"
        return [f"haystack/components/{stem}.py", f"haystack/core/{stem}.py"][:limit]

    def get_file(self, path: str) -> str:
        log.info("github.get_file", repo=self.repo, path=path, mocked=True)
        return (
            f"# (mock) contents of {path} from {self.repo}\n"
            "def run(self, *args, **kwargs):\n"
            "    ...  # implementation elided in mock mode\n"
        )

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

        gh = Github(self.token)
        results = gh.search_code(f"{query} repo:{self.repo_name}")
        paths: list[str] = []
        for item in results[:limit]:
            paths.append(item.path)
        log.info("github.search_code", repo=self.repo_name, query=query, hits=len(paths))
        return paths

    def get_file(self, path: str) -> str:
        repo = self._get_repo()
        contents = repo.get_contents(path, ref=self.base_branch)
        return contents.decoded_content.decode("utf-8")

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


def build_code_host(settings: Settings) -> MockGitHubClient | GitHubClient:
    if settings.use_mocks or not settings.github_token:
        if not settings.use_mocks:
            log.warning("github.no_token", msg="falling back to mock GitHub client")
        return MockGitHubClient(settings.github_repo, settings.github_base_branch)
    return GitHubClient(settings.github_token, settings.github_repo, settings.github_base_branch)
