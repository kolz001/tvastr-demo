"""Tests for the real GitHubClient.search_code path.

Regression coverage for the PyGithub ``PaginatedList`` slicing quirk: the code
search API can report (via ``_couldGrow``/``total_count``) that more results
exist than are actually fetchable, so index-based slice access
(``results[:limit]``) overruns the backing list and raises ``IndexError``.
Direct iteration with a manual cap is the safe access pattern.
"""

from __future__ import annotations

import pytest

from tvastr.integrations import github as gh_mod


class _FakeItem:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakePaginatedList:
    """Mimics PyGithub PaginatedList: iteration is safe, slicing overruns.

    PaginatedList's ``_Slice`` yields elements by integer index and can index
    past the materialised page when the API's reported count is optimistic,
    raising IndexError — exactly the production crash this guards against.
    """

    def __init__(self, items: list[_FakeItem]) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, index):
        if isinstance(index, slice):
            raise IndexError("list index out of range")
        return self._items[index]


def _patch_github(monkeypatch: pytest.MonkeyPatch, items: list[str]) -> None:
    class _FakeGithub:
        def __init__(self, token: str) -> None:
            pass

        def search_code(self, query: str):
            return _FakePaginatedList([_FakeItem(p) for p in items])

    monkeypatch.setattr("github.Github", _FakeGithub)


def test_search_code_does_not_crash_on_paginated_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_github(monkeypatch, ["a.py", "b.py"])
    client = gh_mod.GitHubClient(token="x", repo="owner/repo")
    assert client.search_code("anything", limit=5) == ["a.py", "b.py"]


def test_search_code_caps_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_github(monkeypatch, [f"f{i}.py" for i in range(10)])
    client = gh_mod.GitHubClient(token="x", repo="owner/repo")
    paths = client.search_code("anything", limit=3)
    assert paths == ["f0.py", "f1.py", "f2.py"]


def test_search_code_degrades_gracefully_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from github.GithubException import GithubException

    class _FakeGithub:
        def __init__(self, token: str) -> None:
            pass

        def search_code(self, query: str):
            raise GithubException(422, data={"message": "Validation Failed"}, headers=None)

    monkeypatch.setattr("github.Github", _FakeGithub)
    client = gh_mod.GitHubClient(token="x", repo="owner/repo")
    # A search failure must escalate (empty -> low confidence), not crash the run.
    assert client.search_code("a query the API rejects", limit=5) == []
