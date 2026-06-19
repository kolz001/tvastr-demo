from __future__ import annotations

import httpx

from tvastr.analysis.pr_discovery import PullRequestRef, PrDiff, discover_pr, fetch_pr_diff  # noqa: F401


def _search_response(items):
    return httpx.Response(200, json={"total_count": len(items), "items": items})


def _pr_item(number, title, state, merged_at=None):
    return {
        "number": number,
        "title": title,
        "state": state,
        "pull_request": {"merged_at": merged_at},
        "html_url": f"https://github.com/o/r/pull/{number}",
    }


def test_discover_prefers_open_then_merged_then_closed():
    items = [
        _pr_item(10, "closed unmerged", "closed"),
        _pr_item(20, "merged", "closed", merged_at="2026-01-01T00:00:00Z"),
        _pr_item(30, "open fix", "open"),
    ]
    transport = httpx.MockTransport(lambda req: _search_response(items))
    ref = discover_pr("o/r", 19293, token="t", transport=transport)
    assert ref is not None
    assert ref.number == 30 and ref.state == "open"


def test_discover_returns_none_when_no_prs():
    transport = httpx.MockTransport(lambda req: _search_response([]))
    assert discover_pr("o/r", 1, token="t", transport=transport) is None


def test_discover_returns_none_in_mock_mode():
    assert discover_pr("o/r", 1, token=None, use_mocks=True) is None
    assert discover_pr("o/r", 1, token=None) is None


def test_discover_marks_merged():
    items = [_pr_item(20, "merged", "closed", merged_at="2026-01-01T00:00:00Z")]
    transport = httpx.MockTransport(lambda req: _search_response(items))
    ref = discover_pr("o/r", 1, token="t", transport=transport)
    assert ref.merged is True


def _file(name, patch, status="modified", add=1, dele=0):
    return {"filename": name, "status": status, "additions": add, "deletions": dele, "patch": patch}


def test_fetch_pr_diff_returns_files():
    files = [_file("a.py", "@@ -1 +1 @@\n-old\n+new")]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert isinstance(diff, PrDiff)
    assert diff.files[0].filename == "a.py"
    assert diff.truncated is False


def test_fetch_pr_diff_caps_file_count():
    files = [_file(f"f{i}.py", "@@\n+x") for i in range(40)]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert len(diff.files) == 30
    assert diff.truncated is True


def test_fetch_pr_diff_caps_total_lines():
    big = "\n".join("+line" for _ in range(2000))
    files = [_file("big.py", big)]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert diff.truncated is True
    assert sum(f.patch.count("\n") + 1 for f in diff.files) <= 1500 + 5
