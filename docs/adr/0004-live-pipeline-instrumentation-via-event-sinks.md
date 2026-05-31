# 4. Live pipeline instrumentation via event sinks + SSE

- Status: Accepted
- Date: 2026-05-31

## Context

tvastr's agent is a multi-step LangGraph state machine that calls into a
hybrid LLM router, several tools, and a code host. Until now the only window
into a run was structlog output and the final `PipelineRun` summary — which
makes it easy to demonstrate that the agent "worked" but hard to demonstrate
*how* it worked.

For a portfolio project pitched on the idea that the agent is *inspectable*,
that's the wrong shape: a reviewer who can't see the prompts, routing
decisions, and intermediate diffs has to take the architecture on faith. We
need every interesting moment in the pipeline — node transitions, tool calls,
LLM prompts and responses, routing decisions, redacted payloads, generated
diffs, PR drafts, audit writes — to be observable from outside the process,
and we need it to stream live so a demo is engaging rather than a wall of
log output served at the end.

Two prior options were considered and rejected:

- **Parse structlog output.** Brittle, exposes log-level noise, requires a
  side channel.
- **Bake the UI directly into the pipeline code.** Couples presentation to
  business logic; impossible to test without a UI; impossible to add new
  consumers (e.g. Langfuse, OpenTelemetry) without touching pipeline code.

## Decision

Introduce a small instrumentation layer in `src/tvastr/events.py`:

- A frozen `PipelineEvent` dataclass: `{type, layer, step, payload, timestamp,
  run_id}`. ``type`` is a closed `Literal` enum of ~20 names
  (`pipeline.start`, `agent.node.start`, `router.decide`, `llm.call`,
  `fix.generated`, `pr.dry_run`, etc.). Payload is a JSON-serialisable dict.
- An `EventSink` Protocol with `emit(event)`. Four concrete sinks ship:
  `NullEventSink` (zero overhead — the default when instrumentation is off),
  `ListEventSink` (for tests), `JsonlEventSink` (append-only file), and
  `FanoutEventSink` (broadcast to multiple sinks, swallowing per-sink errors
  so one broken consumer can't break the pipeline).

The pipeline, agent graph, tools, and hybrid router each take an optional
`event_sink` (defaulting to `NullEventSink`) and emit at every significant
step. No node "knows" who is listening; the sink Protocol decouples
production from consumption.

For the live UI surface (`POST /api/run`):

- A `FanoutEventSink` is constructed per request, broadcasting to (a) an
  in-process `queue.Queue` and (b) a `JsonlEventSink` writing to
  `data/runs/<run_id>.jsonl`.
- The pipeline runs in a background thread; the SSE handler is an async
  generator that drains the queue and yields `event: <type>\ndata: <json>\n\n`
  frames until a sentinel marks the run complete.
- **SSE was chosen over WebSocket** because the stream is unidirectional
  (server → client), HTTP-native (no protocol upgrade, works through proxies
  by default), and consumable from vanilla `fetch()` with a `TextDecoder` —
  no library required client-side. WebSocket's full duplex is not needed; the
  user can cancel a run by disconnecting.
- Each persisted JSONL is browsable later via `GET /api/runs` (summary list)
  and `GET /api/runs/{run_id}` (event stream as JSON or replay-shaped SSE).
- Single-issue runs (one user-picked issue) bypass the recurrence threshold:
  the engine is constructed with `recurrence_threshold=1`. The threshold is
  the correct gate for *autonomous* mode (don't open PRs for one-off
  failures); for the UI, the user has explicitly said "fix this one."

## Consequences

- **Inspectability is a first-class feature, not a debugging aid.** The same
  event stream that drives the live UI is what tests assert against and what
  the audit store reflects. There is one truth.
- **Tests get more honest.** `tests/test_events.py` runs the real pipeline
  end-to-end against a `ListEventSink` and asserts that every layer
  contributed its signature events — a stronger contract than asserting on
  the final `PipelineRun` summary.
- **Demos become reproducible.** A persisted run can be linked as a permanent
  URL; the UI's replay path reads the JSONL and animates it back at a fixed
  cadence. Live-demo flakiness (rate limits, network) is decoupled from the
  story you're telling.
- **New consumers are easy to add.** A Langfuse exporter or an OpenTelemetry
  span emitter is "another sink in the fanout" — no pipeline change.
- **Two operational concerns to watch.** (1) `data/runs/` grows unbounded —
  a future `tvastr runs prune` is on the roadmap. (2) Long-lived SSE
  connections need a server-side timeout so a hung pipeline doesn't pin a
  thread forever; for v1 the pipeline self-terminates within a couple of
  minutes in mock mode and a few minutes in live mode, so this is a future
  hardening item, not a v1 blocker.
- **Event payloads can be large.** Full Claude prompts can be several KB;
  retrieved source files larger still. The UI defaults each card to
  collapsed; persisted JSONL files are append-only and not memory-mapped, so
  large payloads cost disk but not RAM. A payload-size cap (truncate
  prompts/responses over 8 KB with a `truncated: true` flag) is a planned
  refinement once we have evidence it matters.
