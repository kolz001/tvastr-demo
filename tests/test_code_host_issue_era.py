from tvastr.integrations.github import DryRunCodeHost, GitHubClient, MockGitHubClient


def test_mock_commit_before_deterministic():
    h = MockGitHubClient("run-llama/llama_index")
    sha = h.commit_before("2024-12-01T00:00:00+00:00")
    assert sha and isinstance(sha, str)
    assert h.commit_before("2024-12-01T00:00:00+00:00") == sha


def test_mock_list_dir_at_ref_returns_entries():
    h = MockGitHubClient("run-llama/llama_index")
    out = h.list_dir_at_ref("a/b", "deadbeef")
    assert out and all(p.startswith("a/b/") for p in out)


def test_dryrun_delegates_issue_era_methods():
    inner = MockGitHubClient("run-llama/llama_index")
    h = DryRunCodeHost(inner, "run-llama/llama_index")
    assert h.commit_before("2024-12-01T00:00:00+00:00") == inner.commit_before(
        "2024-12-01T00:00:00+00:00"
    )
    assert h.list_dir_at_ref("a/b", "ref") == inner.list_dir_at_ref("a/b", "ref")


def test_github_commit_before_uses_until(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    class _Commit:
        sha = "headsha"

    class _Repo:
        def __init__(self):
            self.kwargs = None

        def get_commits(self, **kwargs):
            self.kwargs = kwargs
            return [_Commit()]

    repo = _Repo()
    monkeypatch.setattr(c, "_get_repo", lambda: repo)
    assert c.commit_before("2024-12-01T00:00:00+00:00") == "headsha"
    assert "until" in repo.kwargs


def test_github_commit_before_none_on_error(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(c, "_get_repo", boom)
    assert c.commit_before("2024-12-01T00:00:00+00:00") is None


def test_github_list_dir_at_ref(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    class _Item:
        def __init__(self, p):
            self.path = p

    class _Repo:
        def get_contents(self, path, ref=None):
            assert ref == "myref"
            return [_Item("a/b/x.py"), _Item("a/b/y.py")]

    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    assert c.list_dir_at_ref("a/b", "myref") == ["a/b/x.py", "a/b/y.py"]
