"""Resolution-detector heuristic + API endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tvastr.api import create_app
from tvastr.ingestion.comments import (
    CommentSnippet,
    ResolutionAssessment,
    assess_issue,
    detect_resolution,
    fetch_recent_comments,
)


def _comment(
    body: str,
    *,
    author: str = "user1",
    association: str = "NONE",
    age_days: int = 1,
    is_bot: bool = False,
) -> CommentSnippet:
    return CommentSnippet(
        author=author,
        author_association=association,
        body=body,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        is_bot=is_bot,
    )


# --- detector verdicts -----------------------------------------------------


def test_no_signals_returns_none() -> None:
    r = detect_resolution([_comment("hi"), _comment("any updates?")])
    assert r.confidence == "none"
    assert r.label == ""


def test_maintainer_fixed_in_version_is_high() -> None:
    comments = [
        _comment("does this still happen?"),
        _comment(
            "fixed in v0.10.42 — please upgrade",
            author="logan-markewich",
            association="MEMBER",
        ),
    ]
    r = detect_resolution(comments)
    assert r.confidence == "high"
    assert "fixed in version" in r.signals
    assert "possibly resolved" in r.label


def test_random_user_fixed_in_is_only_medium() -> None:
    r = detect_resolution([_comment("this was fixed for me in v0.10.42")])
    assert r.confidence == "medium"


def test_fixed_in_bare_ordinal_is_not_a_version_signal() -> None:
    """'fixed in 3 places' / 'fixed in 2024' must not read as 'fixed in <version>'
    — a version token requires a v-prefix or a dotted number."""
    r = detect_resolution([_comment("I fixed in 3 places but it still breaks")])
    assert r.confidence == "none"
    assert "fixed in version" not in r.signals


def test_fixed_in_pr_with_hedging_phrase_matches() -> None:
    """Real comment from run-llama/llama_index#19293 — the prompt for this feature."""
    body = (
        "This appears to have been fixed in PR #18876 "
        "(commit 14b8f4f6c — \"Fix Google GenAI token counting behavior\"), "
        "which landed after this issue was filed."
    )
    r = detect_resolution([_comment(body, association="CONTRIBUTOR")])
    assert r.confidence == "medium"
    # Both signals should fire — the explicit "fixed in PR #" and the
    # hedged "this appears to have been fixed".
    assert "fixed in PR" in r.signals
    assert "fixed (asserted)" in r.signals


def test_maintainer_fixed_in_pr_is_high() -> None:
    body = "Resolved in #20000, going out in next release."
    r = detect_resolution([_comment(body, association="MEMBER")])
    assert r.confidence == "high"
    assert "resolved in PR" in r.signals


def test_seems_to_be_fixed_phrasing_matches() -> None:
    r = detect_resolution([_comment("this seems to be resolved by the recent change")])
    assert r.confidence == "medium"
    assert "fixed (asserted)" in r.signals


def test_closed_by_pr_matched() -> None:
    r = detect_resolution(
        [_comment("closed by #19500", association="COLLABORATOR")]
    )
    assert r.confidence == "high"
    assert "closed by PR" in r.signals


def test_duplicate_of_matched() -> None:
    r = detect_resolution([_comment("duplicate of #18900", association="OWNER")])
    assert r.confidence == "high"
    assert "duplicate of" in r.signals


def test_bare_issue_cross_reference_is_not_a_signal() -> None:
    """'see #N' is everyday GitHub cross-referencing, not a resolution claim."""
    r = detect_resolution(
        [_comment("see #456 for a related discussion", association="MEMBER")]
    )
    assert r.confidence == "none"


def test_bots_are_ignored_for_signal_extraction() -> None:
    bot = _comment(
        "This issue has been fixed in v0.10.42 (automated message)",
        author="github-actions[bot]",
        is_bot=True,
    )
    r = detect_resolution([bot])
    assert r.confidence == "none"


def test_old_quiet_thread_is_low_confidence_stale() -> None:
    comments = [
        _comment("any update?", age_days=210),
        _comment("bump", age_days=205),
        _comment("hello?", age_days=200),
    ]
    r = detect_resolution(comments)
    assert r.confidence == "low"
    assert r.label.startswith("stale ·")


def test_recent_quiet_thread_is_not_stale() -> None:
    comments = [
        _comment("hi", age_days=5),
        _comment("bump", age_days=3),
        _comment("any update?", age_days=1),
    ]
    r = detect_resolution(comments)
    assert r.confidence == "none"


def test_issue_body_signal_also_counts() -> None:
    body = "Update: this was fixed in v0.10.42, leaving open for tracking."
    r = detect_resolution([_comment("ok")], issue_body=body)
    assert r.confidence == "medium"


def test_maintainer_signal_wins_over_other_user_signal() -> None:
    comments = [
        _comment("works for me now", association="NONE"),
        _comment("closed by #1234", association="OWNER"),
    ]
    r = detect_resolution(comments)
    assert r.confidence == "high"
    assert r.signals[0] == "closed by PR"


# --- fetcher (mocked transport) -------------------------------------------
#
# GitHub's per-issue comments endpoint only returns ascending order — it has
# no sort/direction params — so "most recent" means paging to the end. These
# tests pin that behavior with a fake 205-comment thread across 3 pages.

_COMMENTS_URL = "https://api.github.com/repos/foo/bar/issues/5/comments"


def _gh_items(start: int, end: int) -> list[dict]:
    return [
        {
            "user": {"login": f"u{i}", "type": "User"},
            "author_association": "NONE",
            "body": f"c{i}",
            "created_at": "2026-06-01T00:00:00Z",
        }
        for i in range(start, end + 1)
    ]


def _link(rels: dict[str, int]) -> str:
    return ", ".join(
        f'<{_COMMENTS_URL}?per_page=100&page={page}>; rel="{rel}"'
        for rel, page in rels.items()
    )


def _paged_thread_handler(request):
    import httpx

    page = int(request.url.params.get("page", "1"))
    if page == 1:
        return httpx.Response(
            200, json=_gh_items(1, 100), headers={"Link": _link({"next": 2, "last": 3})}
        )
    if page == 2:
        return httpx.Response(
            200,
            json=_gh_items(101, 200),
            headers={"Link": _link({"prev": 1, "next": 3, "last": 3})},
        )
    return httpx.Response(200, json=_gh_items(201, 205), headers={"Link": _link({"prev": 2})})


def test_fetch_returns_tail_of_single_page_thread() -> None:
    import httpx

    handler = lambda request: httpx.Response(200, json=_gh_items(1, 4))  # noqa: E731
    out = fetch_recent_comments(
        "foo/bar", 5, token="t", limit=10, transport=httpx.MockTransport(handler)
    )
    assert [c.body for c in out] == ["c1", "c2", "c3", "c4"]


def test_fetch_pages_to_the_end_of_a_long_thread() -> None:
    """205-comment thread, limit=10 → the LAST 10 comments (c196..c205), which
    requires fetching the rel=last page and merging its short tail with the
    previous page."""
    import httpx

    out = fetch_recent_comments(
        "foo/bar", 5, token="t", limit=10, transport=httpx.MockTransport(_paged_thread_handler)
    )
    assert [c.body for c in out] == [f"c{i}" for i in range(196, 206)]


# --- assess_issue ---------------------------------------------------------


def test_assess_in_mock_mode_returns_none_assessment() -> None:
    out = assess_issue("run-llama/llama_index", 1, token=None, use_mocks=True)
    assert isinstance(out, ResolutionAssessment)
    assert out.confidence == "none"


def test_assess_live_uses_issue_body_signals_and_caches() -> None:
    """The issue body is fetched too (it can carry the only resolution signal,
    e.g. 'fixed, leaving open for tracking'), and the assessment is cached so
    per-card UI re-renders don't re-hit GitHub."""
    import httpx

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=_gh_items(1, 2))
        return httpx.Response(
            200, json={"body": "Update: this was fixed in v0.10.42, leaving open for tracking."}
        )

    transport = httpx.MockTransport(handler)
    first = assess_issue("foo/bar", 7042, token="t", transport=transport)
    assert first.confidence == "medium"
    assert "fixed in version" in first.signals

    calls_after_first = calls["n"]
    second = assess_issue("foo/bar", 7042, token="t", transport=transport)
    assert second == first
    assert calls["n"] == calls_after_first  # served from cache, no new requests


def test_assess_degrades_when_comments_fetch_fails() -> None:
    """A GitHub failure on the comments call must not propagate (which would 500
    the resolution endpoint) — degrade to no comments and still assess the body."""
    import httpx

    def handler(request):
        if request.url.path.endswith("/comments"):
            return httpx.Response(503, json={"message": "rate limited"})
        return httpx.Response(200, json={"body": "still investigating, no fix yet"})

    out = assess_issue("foo/bar", 9001, token="t", transport=httpx.MockTransport(handler))
    assert isinstance(out, ResolutionAssessment)
    assert out.confidence == "none"


# --- API endpoint --------------------------------------------------------


client = TestClient(create_app())


def test_resolution_endpoint_returns_none_in_mock_mode() -> None:
    resp = client.get("/api/resolution?repo=run-llama/llama_index&number=8001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["confidence"] == "none"
    assert data["label"] == ""


def test_resolution_endpoint_requires_number() -> None:
    resp = client.get("/api/resolution?repo=foo/bar")
    assert resp.status_code == 422
