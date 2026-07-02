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


# --- POST /api/run pre-creates the run file (no race with an immediate stream) ---


def test_post_creates_run_file_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run file must exist the instant POST returns — not on first emit.

    Otherwise a client that GETs the stream immediately after the 202 (a
    perfectly normal sequence, not a misuse) can race the pipeline thread to
    its first event and hit the stream endpoint's ``not path.exists()`` 404
    for a run that is, in fact, running. We assert existence directly,
    without ever touching the stream endpoint, so this test can't pass by
    accident of the stream retrying/waiting.
    """
    _redirect_runs_dir(monkeypatch, tmp_path)
    client = _client()
    resp = client.post(
        "/api/run", json={"repo": "run-llama/llama_index", "issue_number": 8001, "dry_run": True}
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert (tmp_path / f"{run_id}.jsonl").exists()


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


# --- stream endpoint: liveness-before-read correctness (review Finding 1) ---


def test_stream_delivers_final_event_from_dead_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression pin: liveness must be snapshotted BEFORE the read, not after.

    If liveness were checked after read_text (the pre-fix bug), a thread that
    finishes and deregisters between the read and the check would cause the
    reader to break without ever re-reading the events it wrote on its way
    out. Here the thread is already dead (constructed, started, joined) by
    the time it's registered, and the terminal event is appended only after
    registration — pinning that a dead-thread observation still guarantees
    one full, up-to-date read before the stream gives up.
    """
    _redirect_runs_dir(monkeypatch, tmp_path)
    from tvastr.api.routes import run as run_module

    run_id = "deadthread"
    path = tmp_path / f"{run_id}.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(
        PipelineEvent(
            type="pipeline.start", layer="pipeline", step="pipeline", run_id=run_id, payload={}
        )
    )
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    assert not t.is_alive()
    run_module._IN_FLIGHT[run_id] = t
    sink.emit(
        PipelineEvent(
            type="pipeline.end", layer="pipeline", step="pipeline", run_id=run_id, payload={}
        )
    )
    client = _client()
    with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
        text = "".join(resp.iter_text())
    run_module._IN_FLIGHT.pop(run_id, None)
    assert text.index("event: pipeline.end") < text.index("event: done")


# --- stream endpoint: torn trailing line must not be dropped (review Finding 2) ---


def test_stream_recovers_torn_trailing_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line still being written (no trailing newline yet) must be held back,
    not consumed-and-dropped, so the event isn't lost when the write completes.
    """
    _redirect_runs_dir(monkeypatch, tmp_path)
    from tvastr.api.routes import run as run_module

    run_id = "torn1"
    path = tmp_path / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    start_event = PipelineEvent(
        type="pipeline.start", layer="pipeline", step="pipeline", run_id=run_id, payload={}
    )
    path.write_text(start_event.to_json() + "\n", encoding="utf-8")

    torn_event = PipelineEvent(
        type="detect.cluster", layer="detection", step="cluster", run_id=run_id, payload={}
    )
    torn_json = torn_event.to_json()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(torn_json[:-1])  # write everything but the closing brace, no trailing "\n"

    release = threading.Event()

    def _writer() -> None:
        release.wait(timeout=10)
        # Complete the torn line, then close out the run.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(torn_json[-1:] + "\n")
        JsonlEventSink(path).emit(
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
        for c in resp.iter_text():
            chunks.append(c)
            if "pipeline.start" in "".join(chunks):
                release.set()  # let the writer complete the torn line
            if "event: done" in "".join(chunks):
                break
    text = "".join(chunks)
    run_module._IN_FLIGHT.pop(run_id, None)
    assert text.count("event: detect.cluster") == 1
    assert "event: pipeline.end" in text


def test_stream_of_empty_inflight_run_waits_not_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-created, still-empty run file must attach cleanly (200), never 404.

    This pins the POST-time state the fix produces: the file exists (zero
    complete lines) and the run_id is registered in ``_IN_FLIGHT`` before the
    thread has written anything. The stream must not treat "empty" as
    "unknown" — it should poll while the thread is alive and drain to
    ``done`` once a terminal event lands. Kept deterministic (no sleeping on
    a live writer): the thread is already dead by the time it's registered,
    and the terminal event is appended before attaching, so there's nothing
    to race — this pins "empty file + registry entry -> 200, drains to done"
    without depending on timing.
    """
    _redirect_runs_dir(monkeypatch, tmp_path)
    from tvastr.api.routes import run as run_module

    run_id = "emptyinflight"
    path = tmp_path / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # POST-time state: file created, thread hasn't emitted yet
    assert path.read_text(encoding="utf-8") == ""

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    assert not t.is_alive()
    run_module._IN_FLIGHT[run_id] = t
    JsonlEventSink(path).emit(
        PipelineEvent(
            type="pipeline.end", layer="pipeline", step="pipeline", run_id=run_id, payload={}
        )
    )
    client = _client()
    with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    run_module._IN_FLIGHT.pop(run_id, None)
    assert "event: pipeline.end" in text
    assert text.rstrip().endswith("event: done\ndata: {}")


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


# --- startup sweep gating (review Finding 3) ---


def test_create_app_does_not_sweep_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """TVASTR_SWEEP_ON_STARTUP=false is sealed in conftest.py; building a
    TestClient(create_app()) must never touch data/runs. This is what
    protects the real data/runs directory from the many pre-existing test
    modules that construct TestClient(create_app()) without redirecting the
    runs dir.
    """
    calls: list[object] = []
    monkeypatch.setattr(
        "tvastr.api.app.mark_interrupted_runs",
        lambda *a, **k: calls.append((a, k)) or 0,
    )
    create_app()
    assert calls == []
