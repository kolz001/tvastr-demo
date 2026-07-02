# Run-Lifecycle Productionization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple runs from HTTP clients (job API + attach/reattach stream + interrupted-run sweep), containerize the app (Dockerfile + compose with docker-socket verify sandbox), and add CI (offline suite + image smoke test).

**Architecture:** `POST /api/run` returns `202 {run_id}` immediately; the pipeline thread writes only to the JSONL sink and registers in an in-process `_IN_FLIGHT` registry. One SSE endpoint `GET /api/runs/{id}/stream` replays the JSONL then tails it while the thread lives — serving live attach, mid-run re-attach, and replay of completed runs. A startup sweep appends `pipeline.interrupted` to non-terminal run files. The dashboard migrates to POST-then-attach. The Docker verify sandbox gains a dual-path config so `docker run -v` mounts host-daemon paths when tvastr itself runs in a container (socket mount). Dockerfile/compose/CI are new files.

**Tech Stack:** FastAPI + threads (no new deps), vanilla-JS template, Docker multi-stage build on uv, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-02-run-lifecycle-productionization-design.md`

## Global Constraints

- `POST /api/run` returns without waiting for the pipeline; response body exactly `{"run_id": "<id>"}` with status 202. The per-request queue sink is REMOVED from the run path.
- The stream endpoint's SSE wire format per event is unchanged: `event: <type>\ndata: <event.to_json()>\n\n`; the stream ends with `event: done\ndata: {}\n\n`.
- Terminal events: `pipeline.end`, `pipeline.interrupted`, or a trailing `error`. The sweep is idempotent and never rewrites existing lines — append-only.
- Verify's endpoint (`/api/runs/{id}/verify`) is NOT modified in this arc.
- Sandbox dual-path: with `TVASTR_SANDBOX_HOST_WORK_ROOT` unset, every docker argv is byte-identical to today. `HOST_WORK_ROOT` set while `WORK_ROOT` unset → `ValueError` at settings load.
- Container listens on 8000 internally; compose maps host `8001:8000` (host 8000 belongs to another app).
- No new Python dependencies. `uv run pytest` green (baseline 348 passed, 1 skipped, plus new tests) and `uv run ruff check src tests` clean at every commit.
- Work on branch `feature/run-lifecycle` off `main`.

## File anchors

- `src/tvastr/api/routes/run.py` (270 lines): `_QueueEventSink` (:59), `_start_pipeline_thread` (:106, takes `q`, `finally: q.put(_END_SENTINEL)` :187-188), `POST /api/run` (:199-245, builds queue+fanout, returns StreamingResponse), `GET /api/runs` (:248), `GET /api/runs/{run_id}` (:254).
- `src/tvastr/events.py`: `PipelineEvent` (:68, dataclass with `to_json()`), `JsonlEventSink` (:104), `run_path` (:146), `load_events` (:150), `default_runs_dir`, `new_run_id`.
- `src/tvastr/api/app.py`: `create_app()` (:16); `/app` page route (:37).
- `src/tvastr/verification/sandbox.py`: handle class `__init__(self, root, image)` (:211), `provision` docker_cmd `-v f"{self.root}:/work:rw"` (:256), `run` docker_cmd `-v` (:311), `DockerSandbox` class + `prepare()` (~:330s, `tempfile.mkdtemp`), `build_sandbox` (:351-363).
- `src/tvastr/config.py`: settings flags block (~:46-58).
- `src/tvastr/api/templates/app.html`: `runPipeline(req)` (:617, POSTs and reads the response stream), `replayRun(runId)` (:1030, fetches `/api/runs/{id}` JSON), `summarize(event)` switch (`case "doc.sdk_schema"` present), `updateStory` error-case, `runGen` guard (captured after `resetStory()` in `runPipeline`), `appendEvent`.
- Tests conventions: FastAPI routes are tested with `fastapi.testclient.TestClient` elsewhere in `tests/` (check `tests/test_api*.py` for the fixture idiom); conftest seals make everything mock/offline.

---

### Task 1: Job API + interrupted-run sweep (backend)

**Files:**
- Modify: `src/tvastr/api/routes/run.py`
- Modify: `src/tvastr/events.py` (add `is_terminal_event`, `mark_interrupted_runs`)
- Modify: `src/tvastr/api/app.py` (startup sweep call)
- Test: `tests/test_run_lifecycle.py` (new)

**Interfaces:**
- Produces: `POST /api/run` → `202 {"run_id": str}`; `GET /api/runs/{run_id}/stream` → SSE (catch-up + tail + `done`); `tvastr.api.routes.run._IN_FLIGHT: dict[str, threading.Thread]`; `tvastr.events.is_terminal_event(ev: PipelineEvent) -> bool`; `tvastr.events.mark_interrupted_runs(runs_dir: Path, in_flight: Mapping[str, threading.Thread]) -> int` (returns count marked).
- Consumes: existing `JsonlEventSink`, `run_path`, `load_events`, `new_run_id`, `PipelineEvent`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_lifecycle.py`:

```python
"""Job API + interrupted-run sweep (offline; mock mode via conftest seals)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

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
        sink.emit(PipelineEvent(type=t, layer="pipeline", step="pipeline",
                                run_id=run_id, payload={}))
    return path


# --- POST /api/run returns immediately ---

def test_post_run_returns_run_id_promptly():
    client = _client()
    t0 = time.monotonic()
    resp = client.post("/api/run", json={"repo": "run-llama/llama_index",
                                         "issue_number": 1, "dry_run": True})
    elapsed = time.monotonic() - t0
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"run_id"} and body["run_id"]
    assert elapsed < 5  # mock pipeline may be fast, but we must not block on it


# --- stream endpoint: replay of a completed run ---

def test_stream_replays_completed_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TVASTR_RUNS_DIR", str(tmp_path))
    _write_run(tmp_path, "done1", ["pipeline.start", "ingest.read", "pipeline.end"])
    client = _client()
    with client.stream("GET", "/api/runs/done1/stream") as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    assert "event: pipeline.start" in text
    assert "event: pipeline.end" in text
    assert text.rstrip().endswith("event: done\ndata: {}")


def test_stream_unknown_run_404(tmp_path, monkeypatch):
    monkeypatch.setenv("TVASTR_RUNS_DIR", str(tmp_path))
    client = _client()
    resp = client.get("/api/runs/nope/stream")
    assert resp.status_code == 404


# --- stream endpoint: tails a live run ---

def test_stream_tails_live_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TVASTR_RUNS_DIR", str(tmp_path))
    from tvastr.api.routes import run as run_module

    run_id = "live1"
    path = tmp_path / f"{run_id}.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(PipelineEvent(type="pipeline.start", layer="pipeline",
                            step="pipeline", run_id=run_id, payload={}))
    release = threading.Event()

    def _writer():
        release.wait(timeout=10)
        sink.emit(PipelineEvent(type="detect.cluster", layer="detection",
                                step="cluster", run_id=run_id, payload={}))
        sink.emit(PipelineEvent(type="pipeline.end", layer="pipeline",
                                step="pipeline", run_id=run_id, payload={}))

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

def test_is_terminal_event():
    mk = lambda t: PipelineEvent(type=t, layer="pipeline", step="p",
                                 run_id="x", payload={})
    assert is_terminal_event(mk("pipeline.end"))
    assert is_terminal_event(mk("pipeline.interrupted"))
    assert is_terminal_event(mk("error"))
    assert not is_terminal_event(mk("fix.generated"))


def test_sweep_marks_incomplete_run_once(tmp_path):
    _write_run(tmp_path, "cut", ["pipeline.start", "router.decide"])
    assert mark_interrupted_runs(tmp_path, {}) == 1
    lines = (tmp_path / "cut.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["type"] == "pipeline.interrupted"
    # idempotent: second sweep is a no-op
    assert mark_interrupted_runs(tmp_path, {}) == 0
    assert len((tmp_path / "cut.jsonl").read_text().splitlines()) == len(lines)


def test_sweep_skips_terminal_and_inflight(tmp_path):
    _write_run(tmp_path, "ok", ["pipeline.start", "pipeline.end"])
    _write_run(tmp_path, "err", ["error"])
    _write_run(tmp_path, "flying", ["pipeline.start"])
    marked = mark_interrupted_runs(tmp_path, {"flying": threading.current_thread()})
    assert marked == 0
    for rid in ("ok", "err", "flying"):
        last = (tmp_path / f"{rid}.jsonl").read_text().splitlines()[-1]
        assert json.loads(last)["type"] != "pipeline.interrupted"
```

Note: if `TVASTR_RUNS_DIR` is not an existing settings/env knob, check how
`default_runs_dir()`/`run_path()` resolve the directory and use the mechanism
they support (monkeypatching `tvastr.events.default_runs_dir` and the route
module's import is acceptable); the test intent is a per-test runs dir.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run_lifecycle.py -q`
Expected: FAIL — `ImportError` (`is_terminal_event`, `mark_interrupted_runs`) and 404/handler-missing for `/stream`.

- [ ] **Step 3: Implement `events.py` additions**

Add after `load_events`:

```python
_TERMINAL_TYPES = {"pipeline.end", "pipeline.interrupted", "error"}


def is_terminal_event(event: PipelineEvent) -> bool:
    """True if this event, as the LAST event of a run file, means the run is over."""
    return event.type in _TERMINAL_TYPES


def mark_interrupted_runs(
    runs_dir: Path, in_flight: Mapping[str, "threading.Thread"]
) -> int:
    """Append ``pipeline.interrupted`` to every non-terminal run file.

    Idempotent and append-only. Files whose run_id is in ``in_flight`` are
    skipped. Returns the number of runs marked. Per-file failures are logged
    and never abort the sweep.
    """
    marked = 0
    for path in sorted(runs_dir.glob("*.jsonl")):
        run_id = path.stem
        if run_id in in_flight:
            continue
        try:
            last = None
            for last in load_events(path):  # noqa: B007 — want the last item
                pass
            if last is None or is_terminal_event(last):
                continue
            JsonlEventSink(path).emit(
                PipelineEvent(
                    type="pipeline.interrupted",
                    layer="pipeline",
                    step="pipeline",
                    run_id=run_id,
                    payload={"reason": "server restarted mid-run"},
                )
            )
            marked += 1
        except Exception as exc:
            log.warning("events.sweep.failed", path=str(path), error=str(exc))
    return marked
```

(Import `Mapping` from `collections.abc` and `threading` for the type; match
the module's existing logger. Confirm `PipelineEvent`'s constructor kwargs
against :68 — adjust field names if they differ.)

- [ ] **Step 4: Rewrite the run routes**

In `src/tvastr/api/routes/run.py`:

(a) Delete `_QueueEventSink` and the `queue` import; add at module level:

```python
# Live pipeline threads by run_id. Entries remove themselves when the thread
# finishes, so "in the dict and alive" ⇔ the run is still producing events.
_IN_FLIGHT: dict[str, threading.Thread] = {}
```

(b) `_start_pipeline_thread`: drop the `q` parameter; replace the
`finally: q.put(_END_SENTINEL)` with `finally: _IN_FLIGHT.pop(run_id, None)`;
after constructing the thread, register then start:

```python
    t = threading.Thread(target=_run, daemon=True, name=f"tvastr-run-{run_id}")
    _IN_FLIGHT[run_id] = t
    t.start()
    return t
```

Delete `_END_SENTINEL`.

(c) Replace the `POST /api/run` handler:

```python
@router.post("/api/run", status_code=202)
async def run_pipeline(request: RunRequest) -> JSONResponse:
    """Start a pipeline run and return its id immediately.

    The run executes in a background thread writing data/runs/<id>.jsonl;
    attach to it (live or after the fact) via GET /api/runs/{id}/stream.
    """
    settings = get_settings()
    issue = _fetch_issue(
        request.repo,
        request.issue_number,
        use_mocks=settings.use_mocks,
        token=settings.github_token,
    )
    run_id = new_run_id()
    _start_pipeline_thread(
        request.repo,
        issue,
        dry_run=request.dry_run,
        sink=JsonlEventSink(run_path(run_id)),
        run_id=run_id,
    )
    return JSONResponse({"run_id": run_id}, status_code=202)
```

(d) Add the stream endpoint (place before `GET /api/runs/{run_id}`, which
would otherwise shadow it — FastAPI matches in registration order, so
register `/api/runs/{run_id}/stream` FIRST or rely on the fixed suffix; be
explicit and register it first):

```python
_TAIL_POLL_S = 0.25


@router.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Replay a run's events, then tail while its thread is alive.

    One endpoint for live attach, mid-run re-attach, and replay: reads the
    persisted JSONL (single source of truth) instead of coupling to the
    producing request.
    """
    path = run_path(run_id)
    if not path.exists():
        raise HTTPException(404, f"run {run_id!r} not found")

    async def event_stream() -> AsyncIterator[str]:
        offset = 0
        saw_terminal = False
        while True:
            text = path.read_text(encoding="utf-8")
            chunk, offset = text[offset:], len(text)
            for line in chunk.splitlines():
                if not line.strip():
                    continue
                try:
                    event = PipelineEvent.from_json(line)
                except Exception:
                    log.warning("run.stream.bad_line", run_id=run_id)
                    continue
                yield _event_to_sse(event)
                if is_terminal_event(event):
                    saw_terminal = True
            thread = _IN_FLIGHT.get(run_id)
            if saw_terminal or thread is None or not thread.is_alive():
                break
            await asyncio.sleep(_TAIL_POLL_S)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Tvastr-Run-Id": run_id,
        },
    )
```

(If `PipelineEvent` has no `from_json`, parse with the module's existing
line-parsing helper — `load_events` shows the idiom; extract a
`_parse_event_line(line) -> PipelineEvent | None` in `events.py` if needed
rather than duplicating.)

(e) Update the module docstring (lines 1-9) to describe the job model.

- [ ] **Step 5: Startup sweep in `create_app()`**

In `src/tvastr/api/app.py`, inside `create_app()` after the routers are
included:

```python
    from tvastr.api.routes.run import _IN_FLIGHT
    from tvastr.events import default_runs_dir, mark_interrupted_runs

    runs_dir = default_runs_dir()
    if runs_dir.is_dir():
        marked = mark_interrupted_runs(runs_dir, _IN_FLIGHT)
        if marked:
            log.info("app.sweep.marked_interrupted", count=marked)
```

(Ensure `app.py` has/gets a module logger consistent with the codebase.)

- [ ] **Step 6: Run tests, suite, lint**

Run: `uv run pytest tests/test_run_lifecycle.py -q && uv run pytest -q 2>&1 | tail -1 && uv run ruff check src tests`
Expected: new tests pass; full suite green (348 baseline + new; note some pre-existing tests may exercise `POST /api/run` expecting SSE — update ONLY those assertions to the new `202 {run_id}` contract and note each in your report); lint clean.

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/api/routes/run.py src/tvastr/events.py src/tvastr/api/app.py tests/test_run_lifecycle.py
git commit -m "feat(api): job-model runs — immediate run_id, attach/reattach stream, interrupted-run sweep"
```

---

### Task 2: UI migration to the job API

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (`runPipeline` :617, `replayRun` :1030, `summarize`, `updateStory` error case)

**Interfaces:**
- Consumes: Task 1's `POST /api/run` → `{run_id}` and `GET /api/runs/{id}/stream`.
- Produces: `attachRun(runId, opts)` JS function used by both `runPipeline` and `replayRun`.

- [ ] **Step 1: Extract `attachRun` and migrate `runPipeline`**

Replace `runPipeline`'s fetch-and-parse body: after the existing reset block
(clear pipeline, stage strip, `resetStory()`), it becomes:

```js
  let resp;
  try {
    resp = await fetch("/api/run", {
      method: "POST",
      headers: {"content-type":"application/json"},
      body: JSON.stringify(req),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    currentRunId = body.run_id;
  } catch (e) {
    appendEvent({
      type: "error", layer: "output", step: "fetch",
      payload: {error: String(e)}, timestamp: new Date().toISOString()
    });
    return;
  }
  await attachRun(currentRunId);
```

Add `attachRun` (new function, next to `runPipeline`) — it owns the SSE
fetch-reader loop that `runPipeline` had, pointed at the stream endpoint,
with the same `runGen` staleness guard captured at entry:

```js
// Attach to a run's event stream — live attach, mid-run re-attach after a
// refresh, and replay of completed runs all go through this one path.
async function attachRun(runId) {
  const gen = runGen;
  let resp;
  try {
    resp = await fetch(`/api/runs/${runId}/stream`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (e) {
    if (gen !== runGen) return;
    appendEvent({
      type: "error", layer: "output", step: "fetch",
      payload: {error: String(e)}, timestamp: new Date().toISOString()
    });
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (gen !== runGen) { try { reader.cancel(); } catch {} return; }
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const dataLine = chunk.split("\n").find(l => l.startsWith("data: "));
      if (!dataLine || chunk.startsWith("event: done")) continue;
      try {
        const event = JSON.parse(dataLine.slice(6));
        appendEvent(event);
      } catch {}
    }
    if (gen !== runGen) { try { reader.cancel(); } catch {} return; }
  }
  if (gen === runGen) finalizeRun();
}
```

Preserve `runPipeline`'s existing pre-fetch behavior (gen capture semantics:
`attachRun` captures `runGen` itself, so `runPipeline` no longer needs its own
loop guard). Delete the now-unused reader loop from `runPipeline`.

- [ ] **Step 2: Migrate `replayRun`**

Replace its body's fetch of `/api/runs/${runId}` + `for (const ev of events)
appendEvent(ev)` with the shared path (keep the existing tab-switch, clears,
stage strip, `resetStory()`, and title lines):

```js
  await attachRun(runId);
```

(`attachRun` ends with `finalizeRun()`, so drop the explicit call if the old
body had one after the loop.)

- [ ] **Step 3: Render `pipeline.interrupted`**

(a) In `summarize(event)`, after the `case "doc.sdk_schema"` block:

```js
    case "pipeline.interrupted": return p.reason || "run interrupted";
```

(b) In `updateStory`'s `case "error"` block, extend the trigger so
`pipeline.interrupted` is handled the same way — change the case label
grouping to:

```js
    case "pipeline.interrupted":
    case "error": {
```

and inside, treat interrupted like a non-verify, non-ingestion error (the
existing earliest-pending-row logic applies; the `p.error || "error"` text
falls back — make the message line read
`${p.error || p.reason || "run interrupted"}`; keep existing behavior for
plain errors).

(c) In `appendEvent`, `pipeline.interrupted` needs no divider/chapter entry
(no prefix match — confirm `maybeInsertDivider` ignores it).

- [ ] **Step 4: Verify against the running app**

```bash
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8001/app
curl -s http://localhost:8001/app | grep -c "attachRun"
```
Expected: 200; grep ≥ 3 (definition + two call sites). Report the manual
browser checklist for the controller: start a mock run, refresh mid-run,
re-attach via Past runs → story card rebuilt; replay a completed run; an
interrupted run shows the summarize line.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): attach/reattach runs via the job API (one stream path for live + replay)"
```

---

### Task 3: Sandbox dual-path config (docker-out-of-docker)

**Files:**
- Modify: `src/tvastr/config.py`, `src/tvastr/verification/sandbox.py`
- Test: `tests/test_sandbox_paths.py` (new)

**Interfaces:**
- Produces: `Settings.sandbox_work_root: str | None = None`, `Settings.sandbox_host_work_root: str | None = None` (env `TVASTR_SANDBOX_WORK_ROOT` / `TVASTR_SANDBOX_HOST_WORK_ROOT`), with a model validator raising `ValueError` when host is set but work is not; `DockerSandbox(image=..., work_root: Path | None = None, host_work_root: Path | None = None)`; handles carry `mount_root: Path` used in every `-v` argv.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sandbox_paths.py`:

```python
"""Dual-path sandbox config for docker-out-of-docker (offline; argv only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tvastr.config import Settings
from tvastr.verification.sandbox import DockerSandbox


def test_settings_reject_host_root_without_work_root():
    with pytest.raises(Exception):
        Settings(use_mocks=True, sandbox_host_work_root="/host/sandbox")


def test_docker_handle_mounts_host_path(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    work = tmp_path / "work"; work.mkdir()
    host = Path("/host/visible/sandbox")
    sb = DockerSandbox(image="img", work_root=work, host_work_root=host)
    handle = sb.prepare()
    # files land under the (container-side) work root
    assert str(handle.root).startswith(str(work))
    handle.run(["python", "-V"], timeout_s=5)
    argv = captured["argv"]
    mount = next(a for i, a in enumerate(argv) if argv[i - 1] == "-v")
    # the docker -v mount uses the HOST view with the same per-run dir name
    assert mount == f"{host / handle.root.name}:/work:rw"


def test_docker_handle_default_paths_unchanged(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    sb = DockerSandbox(image="img")
    handle = sb.prepare()
    handle.run(["python", "-V"], timeout_s=5)
    mount = next(a for i, a in enumerate(captured["argv"])
                 if captured["argv"][i - 1] == "-v")
    assert mount == f"{handle.root}:/work:rw"  # byte-identical to today
    handle.discard()
```

(Adjust `handle.run` kwargs to the real signature after reading sandbox.py;
if `prepare()` requires the image to exist or does more setup, monkeypatch
only what the test needs — the assertions are the contract.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sandbox_paths.py -q`
Expected: FAIL — unexpected ctor kwargs / no host-mount behavior.

- [ ] **Step 3: Implement**

(a) `config.py`, after the verify flags block:

```python
    # --- Sandbox paths (docker-out-of-docker) ---
    # Where tvastr creates per-run verify workspaces (default: system temp).
    sandbox_work_root: str | None = None
    # The SAME directory as seen by the host Docker daemon. Required when
    # tvastr itself runs in a container with /var/run/docker.sock mounted:
    # `docker run -v` paths are resolved by the host daemon, not this process.
    sandbox_host_work_root: str | None = None
```

plus a pydantic `model_validator(mode="after")` raising
`ValueError("TVASTR_SANDBOX_HOST_WORK_ROOT requires TVASTR_SANDBOX_WORK_ROOT")`
when host is set and work is not (follow the Settings class's existing
validator style, if any).

(b) `sandbox.py`:
- `DockerSandbox.__init__(self, image: str, work_root: Path | None = None, host_work_root: Path | None = None)` storing both.
- `prepare()`: when `work_root` is set, `work_root.mkdir(parents=True, exist_ok=True)` and `root = Path(tempfile.mkdtemp(prefix="tvastr-verify-", dir=work_root))`; else today's `tempfile.mkdtemp(prefix="tvastr-verify-")`. Compute `mount_root = (host_work_root / root.name) if host_work_root else root` and pass it into the handle.
- Handle `__init__` gains `mount_root: Path` (default `root` for any other constructor callers); BOTH docker argv sites (:256 provision, :311 run) change `f"{self.root}:/work:rw"` → `f"{self.mount_root}:/work:rw"`. File writes (`write_file`, `apply_changes`) keep using `self.root`.
- `build_sandbox` passes `work_root=Path(settings.sandbox_work_root) if settings.sandbox_work_root else None` and likewise `host_work_root` to both `DockerSandbox(...)` constructions.

- [ ] **Step 4: Run tests, suite, lint**

Run: `uv run pytest tests/test_sandbox_paths.py tests/test_verifier.py -q && uv run pytest -q 2>&1 | tail -1 && uv run ruff check src tests`
Expected: all green (existing verifier/sandbox tests unaffected — default path byte-identical); lint clean.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/config.py src/tvastr/verification/sandbox.py tests/test_sandbox_paths.py
git commit -m "feat(verify): dual-path sandbox roots for docker-out-of-docker (host-daemon mounts)"
```

---

### Task 4: Dockerfile, .dockerignore, compose

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`

**Interfaces:**
- Consumes: Task 3's `TVASTR_SANDBOX_WORK_ROOT`/`TVASTR_SANDBOX_HOST_WORK_ROOT`.

- [ ] **Step 1: Write the three files**

`Dockerfile`:

```dockerfile
# Build stage: resolve the locked environment with uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# Runtime stage: slim python + the docker CLI (client only) for the verify
# sandbox, which talks to the HOST daemon via a mounted socket.
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://download.docker.com/linux/static/stable/$(uname -m | sed 's/arm64/aarch64/')/docker-27.3.1.tgz \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src/ src/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Root: the mounted docker socket requires it (documented tradeoff for a
# local/portfolio deployment; a socket-proxy is the hardened alternative).
EXPOSE 8000
CMD ["uvicorn", "tvastr.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

(If `uv sync --no-install-project` errors because the project needs a README
or package metadata earlier, reorder the COPYs — keep the two-phase sync so
dependency layers cache independently of src changes.)

`.dockerignore`:

```
.git/
.venv/
data/
docs/
tests/
.superpowers/
.env
*.jsonl
__pycache__/
*.pyc
```

`docker-compose.yml`:

```yaml
services:
  tvastr:
    build: .
    ports:
      - "8001:8000"   # host 8000 is taken by another local app
    env_file: .env
    environment:
      TVASTR_SANDBOX_WORK_ROOT: /app/data/sandbox
      TVASTR_SANDBOX_HOST_WORK_ROOT: ${PWD}/data/sandbox
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
```

- [ ] **Step 2: Verify build + config**

Run:
```bash
docker build -t tvastr:dev . 2>&1 | tail -3
docker compose config >/dev/null && echo "compose config OK"
docker run --rm -e TVASTR_USE_MOCKS=true -d -p 18000:8000 --name tvastr-smoke tvastr:dev
sleep 3
curl -s -o /dev/null -w "container /health -> %{http_code}\n" http://localhost:18000/health
docker rm -f tvastr-smoke
```
Expected: build succeeds; `compose config OK`; `container /health -> 200`.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "feat(deploy): multi-stage image + compose (socket-mounted verify sandbox, restart policy)"
```

---

### Task 5: CI workflow + smoke script

**Files:**
- Create: `.github/workflows/ci.yml`, `scripts/ci-smoke.sh`

**Interfaces:**
- Consumes: Task 1's endpoints, Task 4's image.

- [ ] **Step 1: Write the smoke script**

`scripts/ci-smoke.sh`:

```bash
#!/usr/bin/env bash
# Boot the built image in mock mode and drive one run to pipeline.end via the
# job API. Fully offline — mock issues, sealed flags.
set -euo pipefail
BASE="${1:-http://localhost:18000}"

for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || { echo "FAIL: /health=$code"; exit 1; }

run_id=$(curl -sf -X POST -H 'content-type: application/json' \
  -d '{"repo":"run-llama/llama_index","issue_number":1,"dry_run":true}' \
  "$BASE/api/run" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run_id=$run_id"

deadline=$((SECONDS + 60))
while [ $SECONDS -lt $deadline ]; do
  if curl -sf --max-time 30 "$BASE/api/runs/$run_id/stream" | grep -q "event: pipeline.end"; then
    echo "OK: run $run_id reached pipeline.end"
    exit 0
  fi
  sleep 2
done
echo "FAIL: run $run_id did not reach pipeline.end within 60s"
exit 1
```

`chmod +x scripts/ci-smoke.sh`.

(Check that mock mode has an issue number the MockGitHubIssuesFetcher serves —
look at its fixtures and use a number it returns; adjust `issue_number` in the
script AND in Task 1's `test_post_run_returns_run_id_promptly` to match.)

- [ ] **Step 2: Write the workflow**

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - run: uv sync --frozen
      - run: uv run ruff check src tests
      - run: uv run pytest -q

  image:
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t tvastr:ci .
      - run: |
          docker run -d --name tvastr-ci -e TVASTR_USE_MOCKS=true -p 18000:8000 tvastr:ci
          ./scripts/ci-smoke.sh http://localhost:18000
      - if: always()
        run: docker logs tvastr-ci || true
```

- [ ] **Step 3: Verify locally**

Run:
```bash
bash -n scripts/ci-smoke.sh && echo "smoke syntax OK"
uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('workflow yaml OK')" 2>/dev/null || python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('workflow yaml OK')"
docker run --rm -d -e TVASTR_USE_MOCKS=true -p 18000:8000 --name tvastr-smoke tvastr:dev && ./scripts/ci-smoke.sh http://localhost:18000; docker rm -f tvastr-smoke
```
Expected: `smoke syntax OK`, `workflow yaml OK`, and the smoke script prints `OK: run ... reached pipeline.end`. (If pyyaml isn't available in either python, validating via `docker compose`-style linting is unnecessary — note it and move on.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml scripts/ci-smoke.sh
git commit -m "feat(ci): offline suite + built-image smoke test through the job API"
```
