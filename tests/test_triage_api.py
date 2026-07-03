import contextlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tvastr.api import create_app
from tvastr.events import JsonlEventSink, PipelineEvent, run_path

client = TestClient(create_app())


# ─── /api/issues ────────────────────────────────────────────────────────────


def test_issues_endpoint_returns_mock_issues_by_default() -> None:
    resp = client.get("/api/issues")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "mock"
    assert data["repo"] == "run-llama/llama_index"
    assert len(data["issues"]) >= 3
    first = data["issues"][0]
    for key in ("number", "title", "url", "labels", "reactions", "thumbs_up", "comments"):
        assert key in first


def test_issues_endpoint_auto_analyze_prs_defaults_false() -> None:
    # The UI must not auto-spend cloud calls on load unless explicitly enabled.
    resp = client.get("/api/issues")
    assert resp.json()["auto_analyze_prs"] is False


def test_issues_sorted_by_thumbs_up_descending() -> None:
    resp = client.get("/api/issues?sort=reactions-%2B1")
    data = resp.json()
    thumbs = [i["thumbs_up"] for i in data["issues"]]
    assert thumbs == sorted(thumbs, reverse=True)


def test_issues_sorted_by_comments() -> None:
    resp = client.get("/api/issues?sort=comments")
    data = resp.json()
    comments = [i["comments"] for i in data["issues"]]
    assert comments == sorted(comments, reverse=True)


# ─── /api/run (job model: POST returns run_id, GET .../stream carries events) ──


def _parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for chunk in body.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[6:]))
    return events


def _run_and_collect(repo: str, issue_number: int) -> list[dict]:
    """POST /api/run then drain its stream endpoint, returning the parsed events."""
    resp = client.post("/api/run", json={"repo": repo, "issue_number": issue_number})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/stream") as stream_resp:
        assert stream_resp.status_code == 200
        assert "text/event-stream" in stream_resp.headers["content-type"]
        assert "X-Tvastr-Run-Id" in stream_resp.headers
        text = "".join(stream_resp.iter_text())
    # Drop the trailing `event: done\ndata: {}` sentinel — it has no "type".
    return [e for e in _parse_sse(text) if "type" in e]


def test_run_streams_full_pipeline_events() -> None:
    # Issue #8001 in the mock fetcher contains "ModuleNotFoundError: ..." so
    # issue_to_events returns at least one event; threshold is bypassed (1).
    events = _run_and_collect("run-llama/llama_index", 8001)
    types = [e["type"] for e in events]
    # Exactly one pipeline.start (carrying run_id in its payload) followed by
    # ingest + cluster + threshold + agent + router + llm + fix + pr. The
    # replayed/tailed stream mirrors the persisted run — no separate
    # synthetic opener.
    assert types.count("pipeline.start") == 1
    start = next(e for e in events if e["type"] == "pipeline.start")
    assert start["payload"]["run_id"]
    assert "ingest.read" in types
    assert "detect.cluster" in types
    assert "threshold.select" in types
    assert "router.decide" in types
    assert "llm.call" in types
    assert "fix.generated" in types
    assert "pr.drafted" in types
    assert "pr.dry_run" in types
    assert "pipeline.end" in types


def test_run_surfaces_error_for_issue_without_signature() -> None:
    # Issue #8050 is a feature request — no error-shaped line in title or body.
    events = _run_and_collect("run-llama/llama_index", 8050)
    types = [e["type"] for e in events]
    assert "error" in types
    error_event = next(e for e in events if e["type"] == "error")
    assert "no error signature" in error_event["payload"]["reason"]


def test_run_404s_on_unknown_issue_in_mock_mode() -> None:
    resp = client.post(
        "/api/run", json={"repo": "run-llama/llama_index", "issue_number": 99999}
    )
    assert resp.status_code == 404


# ─── /api/runs (history) ────────────────────────────────────────────────────


def test_runs_list_and_replay(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
    # Redirect default_runs_dir to a temp path so we don't touch the real data/runs/.
    # The function is imported into both modules; patch both so the route sees it too.
    monkeypatch.setattr("tvastr.events.default_runs_dir", lambda: tmp_path)
    monkeypatch.setattr("tvastr.api.routes.run.default_runs_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "tvastr.api.routes.run.run_path",
        lambda run_id: tmp_path / f"{run_id}.jsonl",
    )

    # Seed one persisted run.
    run_id = "abc123def456"
    path = run_path(run_id, runs_dir=tmp_path)
    sink = JsonlEventSink(path)
    sink.emit(
        PipelineEvent(
            type="pipeline.start",
            layer="ingestion",
            step="open",
            run_id=run_id,
            payload={"repo": "x/y", "issue_number": 1, "issue_title": "T"},
        )
    )
    sink.emit(PipelineEvent(type="pipeline.end", layer="output", step="end", run_id=run_id))

    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert any(r["run_id"] == run_id for r in runs)

    resp = client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    events = resp.json()
    assert [e["type"] for e in events] == ["pipeline.start", "pipeline.end"]
