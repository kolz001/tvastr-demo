# Design Spec: Run-lifecycle productionization (job API, containers, CI)

**Date:** 2026-07-02
**Branch:** `feature/run-lifecycle` (off `main`)
**Status:** Approved design — pending user review of this written spec

## Problem

Three production gaps, all demonstrated live this week:

1. **Runs don't survive the process.** The pipeline already runs in a daemon
   thread writing JSONL (client disconnects are survivable), but when the
   server process died mid-batch (port collision with another app), four
   in-flight runs froze silently — and ~350 older incomplete runs sit in
   `data/runs/` indistinguishable from completed ones. There is also no
   first-class way to attach to a live run: the UI's stream is coupled to the
   POST request, so a browser refresh orphans the view.
2. **No deployment unit.** Docker is used only as the verify sandbox; the app
   itself has no Dockerfile/compose, no restart policy, and its environment
   can drift from dev (the "uv venv has no pip" incident).
3. **No CI.** 350+ tests and ruff exist but nothing runs them on push, and no
   built image is ever smoke-tested.

## Goal & success criteria

- `POST /api/run` returns a `run_id` immediately; any client can attach,
  detach, and re-attach to the run's event stream; a browser refresh mid-run
  re-attaches with the story card rebuilt (demo-visible).
- A server restart marks interrupted runs honestly (`pipeline.interrupted`)
  instead of leaving them ambiguous.
- `docker compose up` runs tvastr with restart policy, persistent `data/`,
  and a working verify sandbox through the host Docker daemon.
- CI runs lint + suite offline and boots the built image in mock mode,
  driving one run to `pipeline.end` via the new endpoints.

## Decisions (locked in brainstorming)

1. **UI migrates in this arc** (not endpoints-only): one way to run things;
   the re-attach UX is the visible payoff. Legacy coupled-SSE behavior on
   `/api/run` is removed.
2. **Event-sourcing stays the single source of truth**: attach/replay/tail is
   one endpoint reading the JSONL; interruption is a synthetic appended
   event, not a sidecar status store.
3. **Verify's endpoint keeps its current coupled-stream shape** this arc
   (recorded follow-up), as do auth, queues/multi-worker, retention, and
   image registry publishing.
4. **Docker-out-of-docker via socket mount** with a dual-path sandbox config
   (below) — not docker-in-docker.

## Design

### 1. Job API (`src/tvastr/api/routes/run.py`)

- `POST /api/run` → starts the pipeline thread exactly as today but with the
  **JSONL file sink only** (the per-request queue sink is removed), registers
  the thread in an in-process registry `_IN_FLIGHT: dict[str, Thread]`, and
  returns immediately: `202 {"run_id": "..."}`.
- `GET /api/runs/{run_id}/stream` → `StreamingResponse` (SSE):
  1. **Catch-up:** stream every existing line of `data/runs/<id>.jsonl` as
     `event: <type>\ndata: <json>` (same wire format as today).
  2. **Tail:** while `_IN_FLIGHT.get(run_id)` is alive, poll the file for new
     lines (async sleep ~0.25 s) and stream them.
  3. **Terminate:** emit `event: done` when a `pipeline.end` (or trailing
     `error`) has been streamed, or when the thread is dead/absent and the
     file is exhausted. Unknown `run_id` (no file) → 404.
  This one endpoint serves live attach, mid-run re-attach, and replay of
  completed runs.
- Registry entries are removed when the pipeline thread finishes (the
  thread's `finally`), so "alive" is accurate.

### 2. Interrupted-run sweep (`src/tvastr/events/` + app startup)

On `create_app()` startup: for each `data/runs/*.jsonl`, if the last event is
non-terminal — terminal being `pipeline.end` OR a trailing `error` event —
append one synthetic event:

```json
{"type": "pipeline.interrupted", "layer": "pipeline", "step": "pipeline",
 "run_id": "<id>", "payload": {"reason": "server restarted mid-run"}}
```

Idempotent (a file already ending in `pipeline.interrupted` is terminal).
Runs currently in `_IN_FLIGHT` are skipped (registry is empty at startup, but
the sweep function takes the registry as an argument for testability). The
~350 error-terminal ingestion runs are untouched.

### 3. UI migration (`src/tvastr/api/templates/app.html`)

- `runPipeline` → `POST /api/run`, parse `{run_id}`, then `attachRun(run_id)`.
- `attachRun(run_id)` = today's fetch-reader SSE loop pointed at
  `GET /api/runs/{run_id}/stream`; `replayRun` becomes a call to the same
  function. Story card, chapter dividers, verify CTA, and the `runGen`
  stale-stream guard all hang off `appendEvent` and work unchanged.
- New `summarize` case for `pipeline.interrupted` (plus story-card handling:
  mark the earliest pending row err with "run interrupted", reusing the
  existing error-row mapping).

### 4. Sandbox dual-path config (`src/tvastr/verification/sandbox.py`, `config.py`)

Two settings:

- `TVASTR_SANDBOX_WORK_ROOT` — directory where tvastr creates per-run work
  dirs (default: current behavior, system temp).
- `TVASTR_SANDBOX_HOST_WORK_ROOT` — the same directory as the **host Docker
  daemon** sees it (default: unset → equals WORK_ROOT; non-container behavior
  is byte-identical).

The Docker sandbox writes files under WORK_ROOT but constructs `docker run
-v <HOST_WORK_ROOT>/<rundir>:/work` mounts. Compose sets the pair to the two
views of `./data/sandbox`.

### 5. Dockerfile + compose

- **Dockerfile** (multi-stage): build stage on the official `uv` image runs
  `uv sync --frozen --no-dev`; runtime stage `python:3.12-slim` + the static
  docker CLI binary (client only); copy venv + `src/`; non-root user where
  the socket allows; `CMD uvicorn tvastr.api.app:create_app --factory --host
  0.0.0.0 --port 8000`.
- **`.dockerignore`**: `data/`, `.venv/`, `.git/`, `docs/`, `tests/`,
  `.superpowers/`, `*.jsonl`.
- **`docker-compose.yml`**: one service; `ports: "8001:8000"`;
  `env_file: .env`; volumes `./data:/app/data` and
  `/var/run/docker.sock:/var/run/docker.sock`;
  `TVASTR_SANDBOX_WORK_ROOT=/app/data/sandbox`,
  `TVASTR_SANDBOX_HOST_WORK_ROOT=${PWD}/data/sandbox`;
  `restart: unless-stopped`.

### 6. CI (`.github/workflows/ci.yml`)

- Job **test**: checkout, install uv, `uv sync`, `uv run ruff check src
  tests`, `uv run pytest -q`. Fully offline (conftest seals).
- Job **image**: `docker build`, run the container with
  `TVASTR_USE_MOCKS=true`, then `scripts/ci-smoke.sh`: `curl /health` → 200;
  `POST /api/run` (mock issue) → `run_id`; poll the stream endpoint until
  `pipeline.end` within 60 s. Verify sandbox not exercised (no socket in CI;
  flag defaults are mock-safe).
- Inert until the repo has a GitHub remote — committed regardless.

## Error handling

- Stream endpoint: unknown run → 404; malformed JSONL line → skipped with a
  log warning; tail poll has no hard timeout while the thread is alive, and
  ends deterministically once it is not.
- Sweep failures on a single file (permission, corrupt) are logged and do not
  abort startup or other files.
- Dual-path sandbox: if `HOST_WORK_ROOT` is set but `WORK_ROOT` is not,
  settings validation fails fast at startup (misconfiguration, not silence).

## Testing (offline)

- Routes: POST returns `run_id` promptly (no pipeline wait); stream replays a
  completed run and ends with `done`; stream tails a live writer thread and
  delivers events appended after attach; 404 for unknown id.
- Sweep: marks a truncated run exactly once (idempotent on second sweep);
  skips `pipeline.end`-terminal and error-terminal files; skips ids present
  in the registry argument.
- Sandbox: with the env pair set, `docker run` argv mounts the host path
  while files are created under the work path; with them unset, argv is
  byte-identical to today.
- UI/browser checklist (manual): start a run, refresh mid-run, re-attach with
  story card rebuilt; replay a completed run via the same path; interrupted
  run shows its badge after a restart.
- CI smoke is itself the container integration test.

## Scope boundary (deferred, recorded)

- Verify endpoint unification with the job model.
- Queue/multi-worker execution, API auth, data retention, registry publish.
