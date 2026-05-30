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

**Test bed:** [Haystack](https://github.com/deepset-ai/haystack) by deepset — a
production-grade, complex Python codebase with rich failure history.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ INGESTION       App Logs → CloudWatch/OpenSearch → EventBridge│
└───────────────────────────────┬─────────────────────────────┘
┌───────────────────────────────▼─────────────────────────────┐
│ DETECTION       Local LLM (Ollama) · clustering · thresholds  │
│                 [sensitive data never leaves the boundary]    │
└───────────────────────────────┬─────────────────────────────┘
┌───────────────────────────────▼─────────────────────────────┐
│ REASONING       LangGraph agent → Claude API                  │
│                 tools: search · retrieve · fix · PR · notify  │
└───────────────────────────────┬─────────────────────────────┘
┌───────────────────────────────▼─────────────────────────────┐
│ OUTPUT          GitHub PR · audit log → OpenSearch · Slack    │
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

`make demo` ingests sample Haystack failure logs, clusters them, selects the
recurring ones, runs the agent on each, and prints the routing decisions and the
(mock) pull request it would open.

### Going live

Copy `.env.example` to `.env`, set `TVASTR_USE_MOCKS=false`, and fill in the
credentials you want to exercise (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
`TVASTR_SLACK_WEBHOOK_URL`). Start local infra with `make up` (OpenSearch +
Dashboards) and an Ollama model with `ollama pull llama3.1`.

## Project layout

```
src/tvastr/
├── config.py          # env-driven settings; use_mocks master switch
├── logging.py         # structured logging (structlog)
├── domain/            # pydantic models shared across layers
├── ingestion/         # log sources (simulated, cloudwatch)  ── INGESTION
├── detection/         # clustering, detector, threshold engine ── DETECTION
├── pii/               # PII / secret redaction at the boundary
├── llm/               # local + cloud clients, hybrid router
├── agent/             # LangGraph state graph + tools         ── REASONING
├── integrations/      # GitHub, Slack (real + mock)
├── storage/           # OpenSearch audit store (+ in-memory)  ── OUTPUT
├── pipeline.py        # end-to-end orchestration
├── api/               # FastAPI app (health, /remediate)
└── cli.py             # `tvastr demo|serve|version`
```

Every integration ships a real client and a mock behind a shared interface; the
`build_*` factories choose based on `Settings`, so the system depends on behavior,
not wiring.

## Roadmap

Full architecture and timeline: [docs/design-doc.pdf](docs/design-doc.pdf).

- **Weeks 1-2** — Foundation & MVP *(this scaffold)*: pipeline, detector, LangGraph
  agent, first auto-PR, ADRs.
- **Weeks 3-4** — Intelligence & hybrid routing: local-model PII classifier,
  multi-turn reasoning, dedup/recurrence tracking, Slack.
- **Weeks 5-6** — AWS deployment & observability: Terraform/CDK, ECS Fargate,
  EventBridge→SQS→Lambda ingestion, Langfuse traces, dashboards, load test.
- **Weeks 7-10** — Polish, docs, open-source launch.

## Tech stack

LangGraph · Claude API · Ollama · FastAPI · OpenSearch · AWS (ECS Fargate + Lambda) ·
Terraform/CDK · Langfuse · PyGithub · Slack webhooks.

## License

Apache-2.0.
