from tvastr.integrations.github import GitHubClient


class _Parent:
    sha = "parentsha"


class _MergedPR:
    merge_commit_sha = "mergesha"


class _UnmergedPR:
    merge_commit_sha = None
    base = type("B", (), {"sha": "current-main-sha"})()


class _Commit:
    @property
    def parents(self):
        return [_Parent()]


def test_merged_pr_returns_merge_parent(monkeypatch):
    c = GitHubClient(token="x", repo="run-llama/llama_index")
    class _Repo:
        def get_pull(self, n): return _MergedPR()
        def get_commit(self, s): return _Commit()
    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    assert c.buggy_parent_sha(123) == "parentsha"


def test_unmerged_pr_returns_none(monkeypatch):
    c = GitHubClient(token="x", repo="run-llama/llama_index")
    class _Repo:
        def get_pull(self, n): return _UnmergedPR()
    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    # No base.sha fallback: an unmerged PR has no valid buggy base on main.
    assert c.buggy_parent_sha(123) is None
