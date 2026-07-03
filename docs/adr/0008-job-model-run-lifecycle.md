# 8. Job-model run lifecycle: event-sourced runs, containers, CI

- Status: Accepted
- Date: 2026-07-02

## Context

ADR-0004 gave the triage UI a live event stream, but `POST /api/run` held
the HTTP connection open for the whole pipeline and streamed directly onto
it. That design has real limits once the UI is meant to survive more than a
happy-path demo:

- **A page refresh loses the run.** The SSE response *is* the run's only
  channel; closing the connection (a refresh, a flaky network, a laptop lid)
  meant there was no way back into a run already in progress.
- **A crashed or restarted server leaves runs in limbo.** Nothing marked an
  in-flight run as anything other than "in progress" forever, even after the
  process that was running it no longer existed.
- **The event log conflated "this run's lifecycle" with "did the last thing
  that happened look terminal."** Verification is user-triggered *after*
  `pipeline.end` and appends `verify.*` events onto the same JSONL — so a
  completed-and-verified run's actual last event is `verify.result`, not
  `pipeline.end`. Any startup logic that judged "is this run done" from only
  the last line in the file would misjudge every verified run.
- **The system only ran as source.** There was no packaged, reproducible way
  to run tvastr — including the Docker-dependent verify sandbox — as a
  single deployable unit, and no automated check that a built image actually
  worked end-to-end rather than merely importing cleanly.

## Decision

**Event-sourced runs are the single source of truth**, and the API becomes a
job model instead of a blocking call:

- `POST /api/run` validates the request, converts the issue to `LogEvent`s,
  **pre-creates the run's JSONL file**, starts the pipeline in a background
  thread, and returns **202 `{"run_id": ...}` immediately** — it does not
  wait for the pipeline. Pre-creating the file closes a race: a client that
  attaches to the stream a moment after the 202 could otherwise 404 a run
  that has, in fact, already started.
- **One endpoint — `GET /api/runs/{run_id}/stream` — serves live attach,
  reattach, and pure replay**, rather than separate code paths for "watching
  a live run" and "replaying a past one." It replays everything persisted in
  the JSONL from the start, then, if the run is still in-flight in this
  process, tails new lines as they're appended, and emits `event: done` once
  a terminal event has been seen or the pipeline thread has exited. A page
  refresh mid-run just re-opens this same endpoint; there is no separate
  "resume" concept to get wrong. Liveness is snapshotted *before* the file is
  read (not after) to avoid a race where the pipeline's very last events
  could be dropped between the check and the read; offsets only advance on
  newline boundaries so a line caught mid-write is retried, not emitted torn.
- **Interrupted runs are swept and marked honestly, not left ambiguous.**
  On startup (`TVASTR_SWEEP_ON_STARTUP`, default on), `mark_interrupted_runs`
  scans persisted runs for ones with no terminal event and no longer in this
  process's in-flight registry, and appends a `pipeline.interrupted` event.
  Terminality is judged by scanning for **any** terminal event
  (`pipeline.end`, `pipeline.interrupted`, `error`) anywhere in the file —
  not just the last one. An earlier version checked only the last event,
  which meant every completed-and-verified run (whose last event is
  `verify.result`, appended after `pipeline.end`) was falsely re-marked
  interrupted on every startup sweep. The in-flight registry is
  per-process by construction (an in-memory map of `run_id → Thread`), so a
  second process pointed at the same `data/` directory cannot see the
  first's live runs as in-flight — it would falsely sweep them. Multi-process
  or containers-alongside-host-dev deployments must disable the sweep on all
  but one process (`TVASTR_SWEEP_ON_STARTUP=false`); a single-process
  deployment should leave it on.
- **The system ships as a container**, and CI proves it: a multi-stage
  `Dockerfile` (uv-resolved build stage; slim runtime stage with a static
  Docker CLI binary for the docker-out-of-docker verify sandbox),
  `docker-compose.yml` (socket-mounted sandbox, `restart: unless-stopped`,
  dual-path sandbox roots for the host-daemon mount), and a two-job GitHub
  Actions pipeline: an offline `test` job (lint + full test suite, no
  Docker), then an `image` job that builds the real Dockerfile and drives
  `scripts/ci-smoke.sh` against the running container — polling `/health`,
  `POST`ing `/api/run`, and polling the stream endpoint for `pipeline.end`.

## Why these choices

- **One stream endpoint, not "live" and "replay" as separate features.**
  Attach, reattach, and replay are the same operation — read what's
  persisted, then keep reading if there's more coming — differing only in
  whether the tail ever produces anything. Building them as one endpoint
  means a refresh is not a special case that can silently diverge from the
  replay path.
- **Pre-creating the run file over a more defensive stream handler.** A
  stream handler that tolerates a missing file for "a little while" trades
  one race for a fuzzier one (how long is "a little while"). Pre-creating
  the file removes the race outright at negligible cost.
- **Any-terminal-event sweep over last-event-only.** The moment a second
  subsystem (verify) is allowed to append to a run's JSONL after the
  pipeline's own terminal event, "the last line" stops being a reliable
  proxy for "is this run over." Scanning the whole file is more work per
  sweep but is actually correct, and sweeps are infrequent (startup-only).
- **Sweep is opt-out per process, not automatically coordinated across
  processes.** Building real cross-process coordination (a lock file, a
  shared registry) for a feature whose only job is "clean up after an
  unclean shutdown" would be a lot of machinery for a rare event; an env var
  that lets an operator say "not this process" is proportionate to the
  actual deployment shapes in play (single container, or host-dev-alongside-
  container-dev on a shared data directory).
- **A built-image smoke test in CI, not just unit tests.** The Dockerfile,
  the compose wiring, and the job API's happy path are exactly the kind of
  thing that passes every unit test while being broken end-to-end (wrong
  `CMD`, a missing runtime dependency, a route that isn't wired into the
  factory the image actually runs). Driving a real mock run through the real
  container's HTTP surface catches the class of bug unit tests structurally
  cannot.

## Consequences

- **The event log is now unambiguously the run's state**, not just its
  history — the sweep logic depends on being able to answer "is this run
  over" purely by reading the file, which requires every subsystem that
  appends to a run's JSONL after the fact (today: verify) to know about and
  respect the terminal-event contract.
- **Multi-process deployments need an explicit sweep policy.** This is a
  real, named operational constraint, not a hidden footgun: `docker-
  compose.yml` sets `TVASTR_SWEEP_ON_STARTUP=false` specifically because a
  developer's host `uvicorn` and the compose container can point at the same
  `./data` directory, and defaults to leaving exactly one sweeper active.
- **The container is now a tested artifact, not just a convenience
  wrapper.** A regression that only shows up when running as a container
  (missing binary, wrong `WORKDIR`, a route registered on the wrong app
  factory) fails CI before merge, at the cost of a slower CI pipeline (an
  image build plus a live HTTP smoke sequence) than the unit-test-only
  baseline.
- **Root in the runtime container is a recorded trade-off, not an
  oversight.** The mounted host docker socket requires it for this
  local/portfolio deployment shape; a socket-proxy sidecar would be the
  hardened alternative for a more exposed deployment, and is out of scope
  here.
