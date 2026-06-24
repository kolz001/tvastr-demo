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


def test_pr_analysis_mock_mode_makes_no_network_call(monkeypatch):
    import tvastr.api.routes.pr as pr_mod

    called = {"diff": False}

    def _boom(*a, **k):
        called["diff"] = True
        raise AssertionError("fetch_pr_diff must not be called in mock mode")

    monkeypatch.setattr(pr_mod, "fetch_pr_diff", _boom)
    resp = client.post("/api/pr-analysis", json={"repo": "o/r", "number": 1})
    assert resp.status_code == 200
    assert resp.json()["pr"] is None
    assert called["diff"] is False
