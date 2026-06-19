from __future__ import annotations

from fastapi.testclient import TestClient

from tvastr.api import create_app

client = TestClient(create_app())


def test_issue_pr_mock_mode_returns_null():
    # Default settings use_mocks=True → discovery returns null, no crash.
    resp = client.get("/api/issue-pr", params={"repo": "o/r", "number": 1})
    assert resp.status_code == 200
    assert resp.json()["pr"] is None


def test_pr_analysis_mock_mode_returns_unavailable():
    resp = client.post("/api/pr-analysis", json={"repo": "o/r", "number": 1})
    assert resp.status_code == 200
    body = resp.json()
    # No PR discoverable offline → analysis unavailable, surfaced honestly.
    assert body["pr"] is None
