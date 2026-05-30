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
make test                # run the test suite
make run                 # start the API at http://localhost:8000  (/docs for OpenAPI)
```

`make demo` ingests sample LlamaIndex failure logs, clusters them, selects the
recurring ones, runs the agent on each, and prints the routing decisions and the
(mock) pull request it would open.

### Dry-run against real source

A dry-run executes the full agent flow — including real Claude calls and real
GitHub reads when keys are present — but suppresses the final PR creation and
prints a unified diff of every proposed change:

```bash
uv run python -m tvastr.cli demo --dry-run
```

This is the recommended gate before any first-time live run.

### Harvest real LlamaIndex issues

The `ingest-github-issues` command turns bug-labelled GitHub issues into the
JSONL shape the simulated source replays:

```bash
uv run python -m tvastr.cli ingest-github-issues \
    --repo run-llama/llama_index \
    --label bug --limit 50 \
    --out data/sample_logs/llamaindex_real.jsonl

uv run python -m tvastr.cli demo \
    --logs data/sample_logs/llamaindex_real.jsonl \
    --dry-run
```

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
├── config.py          # env-driven settings; use_mocks + dry_run master switches
├── logging.py         # structured logging (structlog)
├── domain/            # pydantic models shared across layers
├── ingestion/         # log sources (simulated, cloudwatch, github_issues)  ── INGESTION
├── detection/         # clustering, detector, threshold engine               ── DETECTION
├── pii/               # PII / secret redaction at the boundary
├── llm/               # local + cloud clients, hybrid router
├── agent/             # LangGraph state graph + tools                        ── REASONING
├── integrations/      # GitHub, Slack (real + mock + dry-run decorator)
├── storage/           # OpenSearch audit store (+ in-memory)                 ── OUTPUT
├── pipeline.py        # end-to-end orchestration
├── api/               # FastAPI app (health, /remediate)
└── cli.py             # `tvastr demo|serve|ingest-github-issues|version`
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
