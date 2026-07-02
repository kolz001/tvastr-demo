"""Job API + interrupted-run sweep (offline; mock mode via conftest seals).

Directive resolutions (see task-1 brief):
  1. ``TVASTR_RUNS_DIR`` is not a wired settings/env knob (checked
     ``src/tvastr/config.py`` and ``default_runs_dir``/``run_path`` in
     ``src/tvastr/events.py`` — the latter just returns ``Path("data/runs")``
     with no env override). So per-test isolation instead monkeypatches
     ``tvastr.events.default_runs_dir`` *and* the name as imported into
     ``tvastr.api.routes.run`` (plus ``run_path`` there, since the stream
     route calls the imported name directly) — the same pattern already used
     by ``tests/test_triage_api.py::test_runs_list_and_replay``.
  2. ``PipelineEvent`` has no ``from_json`` (only ``to_json``); the route
     module uses ``events._parse_event_line`` (a small helper factored out of
     ``load_events``'s existing try/except idiom) instead of duplicating it.
  3. ``MockGitHubIssuesFetcher`` serves issue numbers 8001, 8002, 8010, 8050
     (see ``src/tvastr/ingestion/github_issues.py``); the POST test uses
     8001, which already has an SDK repro test elsewhere in the suite.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tvastr.api.app import create_app
from tvastr.events import (
    JsonlEventSink,
    PipelineEvent,
    is_terminal_event,
    mark_interrupted_runs,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _write_run(runs_dir: Path, run_id: str, types: list[str]) -> Path:
    path = runs_dir / f"{run_id}.jsonl"
    sink = JsonlEventSink(path)
    for t in types:
        sink.emit(
            PipelineEvent(type=t, layer="pipeline", step="pipeline", run_id=run_id, payload={})
        )
    return path


def _redirect_runs_dir(monkeypatch: pytest.MonkeyPatch, runs_dir: Path) -> None:
    """Point both the events module and the route module's imported names at runs_dir."""
    monkeypatch.setattr("tvastr.events.default_runs_dir", lambda: runs_dir)
    monkeypatch.setattr("tvastr.api.routes.run.default_runs_dir", lambda: runs_dir)
    monkeypatch.setattr(
        "tvastr.api.routes.run.run_path",
        lambda run_id: runs_dir / f"{run_id}.jsonl",
    )


# --- POST /api/run returns immediately ---


def test_post_run_returns_run_id_promptly() -> None:
    client = _client()
    t0 = time.monotonic()
    # Issue #8001 in the mock fetcher contains "ModuleNotFoundError: ..." so
    # issue_to_events returns at least one event.
    resp = client.post(
        "/api/run", json={"repo": "run-llama/llama_index", "issue_number": 8001, "dry_run": True}
    )
    elapsed = time.monotonic() - t0
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"run_id"} and body["run_id"]
    assert elapsed < 5  # mock pipeline may be fast, but we must not block on it


# --- stream endpoint: replay of a completed run ---


def test_stream_replays_completed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_runs_dir(monkeypatch, tmp_path)
    _write_run(tmp_path, "done1", ["pipeline.start", "ingest.read", "pipeline.end"])
    client = _client()
    with client.stream("GET", "/api/runs/done1/stream") as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "event: pipeline.start" in text
    assert "event: pipeline.end" in text
    assert text.rstrip().endswith("event: done\ndata: {}")


def test_stream_unknown_run_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_runs_dir(monkeypatch, tmp_path)
    client = _client()
    resp = client.get("/api/runs/nope/stream")
    assert resp.status_code == 404


# --- stream endpoint: tails a live run ---


def test_stream_tails_live_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_runs_dir(monkeypatch, tmp_path)
    from tvastr.api.routes import run as run_module

    run_id = "live1"
    path = tmp_path / f"{run_id}.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(
        PipelineEvent(
            type="pipeline.start", layer="pipeline", step="pipeline", run_id=run_id, payload={}
        )
    )
    release = threading.Event()

    def _writer() -> None:
        release.wait(timeout=10)
        sink.emit(
            PipelineEvent(
                type="detect.cluster",
                layer="detection",
                step="cluster",
                run_id=run_id,
                payload={},
            )
        )
        sink.emit(
            PipelineEvent(
                type="pipeline.end", layer="pipeline", step="pipeline", run_id=run_id, payload={}
            )
        )

    t = threading.Thread(target=_writer, daemon=True)
    run_module._IN_FLIGHT[run_id] = t
    t.start()
    client = _client()
    chunks: list[str] = []
    with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
        it = resp.iter_text()
        for c in it:
            chunks.append(c)
            if "pipeline.start" in "".join(chunks):
                release.set()  # events appended AFTER attach must still arrive
            if "event: done" in "".join(chunks):
                break
    text = "".join(chunks)
    assert "event: detect.cluster" in text
    assert "event: pipeline.end" in text
    run_module._IN_FLIGHT.pop(run_id, None)


# --- terminal detection + sweep ---


def test_is_terminal_event() -> None:
    def mk(t: str) -> PipelineEvent:
        return PipelineEvent(type=t, layer="pipeline", step="p", run_id="x", payload={})

    assert is_terminal_event(mk("pipeline.end"))
    assert is_terminal_event(mk("pipeline.interrupted"))
    assert is_terminal_event(mk("error"))
    assert not is_terminal_event(mk("fix.generated"))


def test_sweep_marks_incomplete_run_once(tmp_path: Path) -> None:
    _write_run(tmp_path, "cut", ["pipeline.start", "router.decide"])
    assert mark_interrupted_runs(tmp_path, {}) == 1
    lines = (tmp_path / "cut.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["type"] == "pipeline.interrupted"
    # idempotent: second sweep is a no-op
    assert mark_interrupted_runs(tmp_path, {}) == 0
    assert len((tmp_path / "cut.jsonl").read_text().splitlines()) == len(lines)


def test_sweep_skips_terminal_and_inflight(tmp_path: Path) -> None:
    _write_run(tmp_path, "ok", ["pipeline.start", "pipeline.end"])
    _write_run(tmp_path, "err", ["error"])
    _write_run(tmp_path, "flying", ["pipeline.start"])
    marked = mark_interrupted_runs(tmp_path, {"flying": threading.current_thread()})
    assert marked == 0
    for rid in ("ok", "err", "flying"):
        last = (tmp_path / f"{rid}.jsonl").read_text().splitlines()[-1]
        assert json.loads(last)["type"] != "pipeline.interrupted"
