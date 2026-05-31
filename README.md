# tvastr

> **Autonomous Code Remediation Agent** — watches application logs for recurring
> failures, traces each one to its root cause in a GitHub repository, and opens a
> pull request that fixes it. Sensitive data is handled by **local** models; complex
> reasoning by **Claude**.

Named for *Tvastr*, the divine craftsman of Vedic myth who shapes and repairs.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

---

## Why

Production incidents follow patterns. The same failure recurs, someone eventually
notices, traces it, writes a fix, and opens a PR. tvastr automates that loop — while
respecting a hard constraint real systems have: **logs contain PII and secrets that
must not leave the local boundary.** It solves both with a hybrid LLM router that
keeps sensitive parsing local (Ollama) and escalates only redacted, non-sensitive
context to the cloud (Claude).

**Test bed:** [LlamaIndex](https://github.com/run-llama/llama_index) by
run-llama — a complex, actively-maintained Python codebase whose post-v0.10
namespace split produces a rich stream of mechanically-fixable bugs (deprecated
imports, `ServiceContext` → `Settings` migrations, schema/type mismatches between
nodes, vector-store dimension mismatches, async/sync errors). See
[ADR-0003](docs/adr/0003-switch-testbed-from-haystack-to-llamaindex.md) for the
rationale.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ INGESTION       App Logs → CloudWatch/OpenSearch → EventBridge│
│                 (or: GitHub-issues harvester for the demo)    │
└───────────────────────────────┬──────────────────────────────┘
┌───────────────────────────────▼──────────────────────────────┐
│ DETECTION       Local LLM (Ollama) · clustering · thresholds  │
│                 [sensitive data never leaves the boundary]    │
└───────────────────────────────┬──────────────────────────────┘
┌───────────────────────────────▼──────────────────────────────┐
│ REASONING       LangGraph agent → Claude API                  │
│                 tools: search · retrieve · fix · PR · notify  │
│                 (with structured JSON fix gen + dry-run mode) │
└───────────────────────────────┬──────────────────────────────┘
┌───────────────────────────────▼──────────────────────────────┐
│ OUTPUT          GitHub PR (draft, fork-only by default)       │
│                 · audit log → OpenSearch · Slack · Langfuse   │
└──────────────────────────────────────────────────────────────┘
```

**Hybrid routing** ([ADR-0002](docs/adr/0002-hybrid-local-cloud-llm-routing.md)):

| Task | Tier | Why |
| --- | --- | --- |
| Log parsing / clustering | Local (Ollama) | sensitive data, no external calls |
| Failure summarization | Local | PII redaction before escalation |
| Root-cause reasoning | Cloud (Claude) | complex multi-step thinking |
| Code-fix generation | Cloud | high accuracy needed |
| PR description | Cloud | natural-language quality |
| Threshold / dedup | Rule-based | speed; no LLM needed |

Raw sensitive data never crosses to the cloud tier — it is redacted at the boundary,
and every routing decision is recorded in the audit trail.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). **No API keys or AWS
account needed** — the project runs fully offline in mock mode by default.

```bash
uv sync --extra dev      # create venv + install
make demo                # run the pipeline against bundled sample logs
make test                # run the test suite (69+ tests, <1s)
make run                 # start the server  (visit /app for the triage UI, /docs for OpenAPI)
```

`make demo` ingests sample LlamaIndex failure logs, clusters them, selects the
recurring ones, runs the agent on each, and prints the routing decisions and the
(mock) pull request it would open.

### Interactive triage UI

```bash
make run
open http://localhost:8000/app
```

The page is a single-file vanilla-JS app served by the same FastAPI server. It
gives you:

- **Top-N GitHub issues** for any repo, ranked by 👍 thumbs-up, comments, total
  interactions, or recency. GitHub's Search API does the sorting server-side.
- **One-click "Apply fix" → live pipeline stream.** Every node transition, tool
  call, routing decision, LLM prompt + response, generated diff, and PR draft
  appears as a card in the right pane as it happens. Cards are summarised by
  default; click to expand the full prompt/response/diff/JSON.
- **Persisted runs.** Each run is written to `data/runs/<run_id>.jsonl`. The
  *Past runs* tab lists every previous run; clicking one replays the event
  stream. Demos are reproducible — link a `run_id` and anyone can re-watch the
  same agent run later.
- **Auto-detect mode.** Live (real GitHub + real Claude) if `ANTHROPIC_API_KEY`
  and `GITHUB_TOKEN` are set; mock otherwise. The header badge shows which.
- **Dry-run by default.** The UI never opens a real PR. Use the CLI
  (`tvastr demo`) when you're ready to flip the safety off.

The architecture and trade-offs are written up in
[ADR-0004](docs/adr/0004-live-pipeline-instrumentation-via-event-sinks.md) and
[docs/design-doc.md §4.6](docs/design-doc.md).

### The agent verifies its own fixes

A diff isn't a fix — the loop closes only when we've shown the failure goes
away. After the agent produces a `pr.dry_run` in the triage UI, a **"Verify
this fix"** button appears on the timeline. Click it and tvastr:

1. **Synthesizes a reproducer** — extracts a runnable Python block from the
   issue body when one exists, else asks Claude to write one from the
   exception class + traceback.
2. **Spins up a sandbox** — Docker preferred (`--read-only --network=none
   --cap-drop=ALL`), `uv`-style subprocess as a fallback.
3. **Runs baseline → applies patch → re-runs** the reproducer, then
   optionally runs a scoped slice of the project's own tests as a
   regression check.
4. **Emits a verdict** with an explicit oracle label, streamed into the same
   timeline:
   - `verified_via_reproducer` — the original exception no longer fires.
   - `verified_via_scoped_tests` — repro ambiguous, but scoped tests pass.
   - `still_broken` / `regression` / `no_repro` — surfaced as the same UX
     prominence as a green; we don't quietly hide failures.

The verdict appears in the **Past runs** table's `Verified?` column so a
run's outcome is visible at a glance later. See
[ADR-0005](docs/adr/0005-verify-fix-loop.md) for the design rationale and
[docs/design-doc.md §4.7](docs/design-doc.md) for the full loop.

**Honest limits worth knowing**

- The verifier runs synthesized Python — Docker's sandbox flags are
  load-bearing. The subprocess fallback is weaker; use it for development,
  not for verifying patches from untrusted sources.
- `no_repro` is common when the issue body has no code and the traceback is
  thin — the verdict is recorded honestly rather than upgraded.
- Each verification ≈ 1 Claude call (~$0.01–0.05) + ~30s of sandbox time
  (longer on Docker cold pull). The UI shows the cost before firing.

To use Docker, build the LlamaIndex base image once:

```bash
docker build -t tvastr-verify:llamaindex -f verification/Dockerfile.llamaindex .
```

### Dry-run against real source

A dry-run executes the full agent flow — including real Claude calls and real
GitHub reads when keys are present — but suppresses the final PR creation and
prints a unified diff of every proposed change:

```bash
uv run python -m tvastr.cli demo --dry-run
```

This is the recommended gate before any first-time live run.

### Bring your own logs (no proprietary SaaS required)

tvastr's only contract with the outside world is **JSONL of `LogEvent`** —
`{service, severity, message, stack_trace, attributes}`. Anything that can
produce that shape plugs in. Three concrete paths, in order of "least infra
needed":

**1. Pipe via stdin** — adapt with `jq` in one line:

```bash
# Your app already writes structured JSON logs? Map the fields:
cat /var/log/myapp.log \
  | jq -c '{service: .app, severity: .level, message: .msg, stack_trace: .trace}' \
  | uv run python -m tvastr.cli demo --logs - --dry-run
```

**2. From a Grafana Loki cluster** — OSS, Apache-2.0, self-hostable:

```bash
TVASTR_LOKI_URL=http://localhost:3100 \
TVASTR_LOKI_QUERY='{app="myapp"} |= "Error"' \
uv run python -m tvastr.cli ingest-loki --out data/sample_logs/loki.jsonl

uv run python -m tvastr.cli demo --logs data/sample_logs/loki.jsonl --dry-run
```

**3. From a GitHub repo's bug tracker** — useful when you don't have a
production log stream yet:

```bash
uv run python -m tvastr.cli ingest-github-issues \
    --repo run-llama/llama_index \
    --label bug --limit 50 \
    --out data/sample_logs/llamaindex_real.jsonl

uv run python -m tvastr.cli demo \
    --logs data/sample_logs/llamaindex_real.jsonl \
    --dry-run
```

Adding a new source is ~30 LOC implementing the `LogSource` Protocol — see
[docs/design-doc.md](docs/design-doc.md) §4.1.

### Going live

Copy `.env.example` to `.env`, set `TVASTR_USE_MOCKS=false`, and fill in the
credentials you want to exercise (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
`TVASTR_SLACK_WEBHOOK_URL`). **Point `TVASTR_GITHUB_REPO` at your own fork** —
the agent refuses to open PRs on a repo you don't own unless
`TVASTR_ALLOW_UPSTREAM=true`. Start local infra with `make up` (OpenSearch +
Dashboards) and an Ollama model with `ollama pull llama3.1`.

## Project layout

```
src/tvastr/
├── config.py          # env-driven settings (use_mocks, dry_run, audit_backend)
├── logging.py         # structured logging (structlog)
├── events.py          # PipelineEvent + EventSink protocol (Null/List/Jsonl/Fanout)
├── domain/            # pydantic models shared across layers
├── ingestion/         # log sources: simulated, stdin, github_issues, loki      ── INGESTION
├── detection/         # clustering, detector, threshold engine                   ── DETECTION
├── pii/               # PII / secret redaction at the boundary
├── llm/               # local + cloud clients, hybrid router
├── agent/             # LangGraph state graph + tools                            ── REASONING
├── integrations/      # GitHub, Slack (real + mock + dry-run decorator)
├── storage/           # audit stores: in-memory, file (default), OpenSearch       ── OUTPUT
├── pipeline.py        # end-to-end orchestration (event-sink instrumented)
├── verification/      # sandbox + reproducer + verifier                        ── VERIFY
├── api/
│   ├── app.py         # FastAPI factory
│   ├── routes/        # health, remediate, issues, run, verify (SSE + replay)
│   └── templates/
│       └── app.html   # single-file triage + verification UI
└── cli.py             # tvastr demo|serve|ingest-github-issues|ingest-loki|version
```

Every integration ships a real client and a mock behind a shared interface; the
`build_*` factories choose based on `Settings`, so the system depends on behavior,
not wiring.

## Roadmap

Full architecture and timeline: [docs/design-doc.md](docs/design-doc.md).

- **Weeks 1-2** — Foundation & MVP: pipeline, detector, LangGraph agent,
  dry-run mode, structured fix generation, ADRs *(largely complete)*.
- **Weeks 3-4** — Intelligence & hybrid routing: local-model PII classifier,
  multi-turn reasoning, stack-trace extraction from issue bodies, real Anthropic
  integration validated against LlamaIndex issues.
- **Weeks 5-6** — AWS deployment & observability: Terraform/CDK, ECS Fargate,
  EventBridge→SQS→Lambda ingestion, Langfuse traces, dashboards, load test.
- **Weeks 7-10** — Polish, docs, open-source launch.

## Tech stack

LangGraph · Claude API · Ollama · FastAPI · OpenSearch · AWS (ECS Fargate + Lambda) ·
Terraform/CDK · Langfuse · PyGithub · Slack webhooks.

## License

Apache-2.0.
