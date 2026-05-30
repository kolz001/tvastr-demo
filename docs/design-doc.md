# tvastr — Autonomous Code Remediation Agent

**Design & 2.5-Month Roadmap (LlamaIndex testbed)**

Status: Draft · Author: Nikhil Koli · Last updated: 2026-05-30

> Supersedes the original Haystack-era `design-doc.pdf` in this directory. See [§9 Migration notes](#9-migration-notes-from-haystack-version) for what changes when the testbed is LlamaIndex instead of Haystack.

---

## 1. Vision

**tvastr** is an open-source autonomous code remediation agent. It watches a production application's logs for *recurring* failure signatures, traces each one to its root cause in a target GitHub repository, generates a minimal code fix, and opens a pull request — without a human in the loop. A hybrid LLM routing layer keeps sensitive log data on local models while sending only sanitized, structured payloads to a frontier model for reasoning and code generation.

The bet is that most production bugs *recur* — and that the most expensive part of fixing them isn't the code change itself but the human time spent reading logs, finding the right file, and writing the PR. An agent that does this end-to-end (with a confidence gate and a dry-run mode) is a force multiplier for a small engineering team and a credible portfolio artifact for a Tech Manager / Principal Engineer.

The agent is named for *Tvastr*, the Vedic divine craftsman who shapes and repairs.

## 2. Why LlamaIndex as the testbed

The testbed needs to be a production-grade open-source Python project with (a) a large, complex codebase, (b) a rich GitHub bug history with reproducible tracebacks, (c) an active community whose pain points the agent can plausibly address. **LlamaIndex (`run-llama/llama_index`)** fits all three:

- **Real-world surface area.** LlamaIndex is one of the two dominant LLM application frameworks (alongside LangChain). Production users build RAG systems, agents, and document pipelines on it, and the issue tracker reflects that complexity — schema mismatches, vector-store integration errors, LLM-provider API failures, async/sync misuse, deprecated-import errors after the v0.10 namespace split.
- **Issues with tracebacks.** A large fraction of bug-labelled issues paste full Python tracebacks — exactly the signal the detection and code-retrieval layers depend on.
- **Generalizable lessons.** Patterns the agent learns to handle on LlamaIndex (schema validation errors, deprecated APIs, provider-side rate limits) recur in nearly every Python OSS project. The architecture is testbed-agnostic; LlamaIndex is the lighthouse.
- **Active maintenance.** Frequent releases mean new bug shapes appear continuously, which makes the agent's value visible over time rather than one-shot.

The agent never opens PRs against the *upstream* `run-llama/llama_index` without explicit opt-in — by default it targets a user-owned fork (see [§4 Architecture, output stage](#4-architecture)).

## 3. Goals & non-goals

**Goals**
1. End-to-end autonomous flow: ingest log → cluster → analyze → fix → PR → audit, with no human step on the happy path.
2. Hybrid LLM routing that is **privacy-by-construction**: sensitive log data never crosses the local boundary unredacted.
3. **Dry-run** as a first-class mode so every change is reviewable before it touches GitHub.
4. Production-grade observability: every routing decision and outcome is audited in OpenSearch and traced in Langfuse.
5. Generalizable: swapping the testbed from LlamaIndex to a different Python OSS project is a config change, not a rewrite.

**Non-goals**
1. Fixing bugs that require deep product knowledge or cross-repo coordination — out of scope for v1.
2. Replacing human review on the *upstream* repo — the agent opens PRs on a fork; a human still merges.
3. Multi-language support — Python only in v1.
4. Real-time response — the loop is minutes-to-hours, not seconds.

## 4. Architecture

A four-layer pipeline. Every layer is wired behind a Protocol so the offline (mock) and live (real) variants are interchangeable.

```
┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────────┐    ┌─────────────────┐
│   1. Ingestion      │ →  │   2. Detection       │ →  │   3. Agentic       │ →  │   4. Output     │
│                     │    │                      │    │      reasoning     │    │                 │
│  • CloudWatch/      │    │  • Local LLM         │    │  • LangGraph ReAct │    │  • GitHub PR    │
│    OpenSearch       │    │    (Ollama)          │    │    state machine   │    │    (draft, on   │
│  • EventBridge →    │    │  • Fingerprint       │    │  • Tools: search,  │    │    user fork)   │
│    SQS → Lambda     │    │    clustering        │    │    retrieve, gen   │    │  • OpenSearch   │
│  • SimulatedLog-    │    │  • Threshold engine  │    │    fix, draft PR   │    │    audit index  │
│    Source for dev   │    │  • PII redaction     │    │  • Confidence gate │    │  • Slack notify │
│  • GitHub-issues    │    │                      │    │  • Dry-run wrapper │    │  • Langfuse     │
│    harvester        │    │                      │    │                    │    │    traces       │
└─────────────────────┘    └──────────────────────┘    └────────────────────┘    └─────────────────┘
```

### 4.1 Ingestion

Sources are pulled (or pushed) into a uniform `LogEvent` shape (`service`, `severity`, `message`, `stack_trace`, `attributes`, `source`).

- **Production:** application logs land in CloudWatch (or OpenSearch); EventBridge routes failure events to an SQS queue; a Lambda batches and calls the agent.
- **Local development:** `SimulatedLogSource` replays a bundled JSONL of LlamaIndex-flavoured failure events. Same shape as production.
- **GitHub issues (for the demo):** the `tvastr ingest-github-issues` CLI command harvests bug-labelled issues from a target repo (default `run-llama/llama_index`), extracts error signatures from the body, and writes them out as a JSONL the simulated source can replay. This is how the demo gets real-world signal without needing a production deployment.

### 4.2 Detection

- **Fingerprint clustering.** Each event is normalised (UUIDs, hex, numbers, quoted literals stripped) and hashed with `service|exception_type|normalized_message`. The same failure with different runtime IDs collapses into one `FailurePattern`. Today's normaliser is regex-based and fast; a learned classifier is on the roadmap.
- **PII / sensitivity classification.** A regex scan over each event tags `Sensitivity.SENSITIVE` if it carries emails, API keys, credentialed URLs, or IPs. This is the input to the router (see §5). A local LLM classifier is the planned upgrade.
- **Threshold engine.** A pattern is escalated to the agent only when it recurs ≥ N times (default 3) within a dedup window. Patterns already handled (PR opened) are recorded so the agent doesn't re-fix what's already in flight.

### 4.3 Agentic reasoning

A LangGraph ReAct state machine. Each node is a focused step; tools are thin functions over an `AgentContext` (router, code host, notifier).

```
START
  └─→ investigate  ──(extract files from stack trace; if none, search code host)
       └─→ reason_root_cause  ──(cloud LLM call; sets confidence)
            └─→ confidence gate
                 ├─ act  ──→ generate_fix  ──→ draft_pr  ──→ open_pr  ──→ notify  ──→ END
                 └─ skip ─────────────────────────────────────────────→ notify  ──→ END
```

The **confidence gate** is the ReAct decision point: only act when confidence ≥ a threshold; otherwise escalate to a human via Slack with the pattern + root-cause analysis. This is the safety valve that keeps low-quality fixes off the PR queue.

The **fix generation** node asks Claude for *structured JSON* describing surgical `search`/`replace` operations against the retrieved source files. Each operation is validated (search string must exist and be unique in the file), applied via `str.replace`, and rendered as a unified diff. If parsing or validation fails, the pipeline degrades to a placeholder rather than crash — the audit log captures the degradation. This is the difference between "PR opens with a comment block" (placeholder) and "PR opens with a real, reviewable diff" (production).

### 4.4 Output

- **GitHub PR.** Real client uses PyGithub: creates a branch off `base`, commits each `FileChange`, opens a PR (always `draft=True` in live mode). Default target repo is user-configurable; safety guardrail refuses non-owned upstream repos unless `TVASTR_ALLOW_UPSTREAM=true`.
- **OpenSearch audit index.** Every remediation run writes an immutable `AuditRecord` (pattern title, routing decisions, root-cause summary, PR URL, outcome). This is what makes the agent inspectable and the cost-of-being-wrong recoverable.
- **Slack notification.** One message per outcome (PR opened / dry-run / human review needed). Format makes the outcome obvious at a glance.
- **Langfuse traces.** Every LLM call is traced with prompt, response, latency, cost, and routing target.

### 4.5 Dry-run mode

A `DryRunCodeHost` decorator wraps whichever code host (real or mock) is built. Reads (`search_code`, `get_file`) pass through; `open_pull_request` is intercepted — the draft is captured for inspection and a `PullRequestResult(dry_run=True, created=False)` is returned. The agent's downstream nodes (`_open_pr`, `_notify`) branch on `dry_run` to produce a distinct outcome. The CLI's `--dry-run` prints the proposed title, branch, target files, and a unified diff per change.

Dry-run is mandatory for any first-time live run against a real repo. The flow:
1. Run with `TVASTR_USE_MOCKS=false`, `TVASTR_DRY_RUN=true`, `ANTHROPIC_API_KEY=...` — fetch real source, get real Claude analysis, see real diffs, **no PR is opened**.
2. Eyeball 10–20 dry-run outputs. Tune prompts, thresholds, code search.
3. Only when confidence in the output is high, flip `TVASTR_DRY_RUN=false` against a **fork** (never the upstream).

## 5. Hybrid local/cloud LLM routing

Application logs routinely contain PII and secrets. Sending raw logs to a cloud LLM is a privacy and compliance risk; using only a local model gives up the reasoning quality that root-cause analysis and code generation need. **Route work by data sensitivity, not by convenience.**

| Task | Tier | Rationale |
| --- | --- | --- |
| Log parsing / clustering | Local (Ollama, e.g. Llama 3) | Sensitive data, no external calls |
| Failure summarisation | Local | PII redaction happens here pre-escalation |
| Root-cause reasoning | Cloud (Claude) | Complex multi-step thinking |
| Code-fix generation | Cloud | High accuracy needed |
| PR description writing | Cloud | Natural-language quality |
| Threshold / dedup checks | Rule-based | Speed; no LLM needed |

Two invariants:

1. **Sensitivity is classified locally.** A regex PII scan today; a local-model classifier in a later milestone. Classification never crosses the local boundary.
2. **Anything escalated to the cloud tier is redacted first.** Raw sensitive data never leaves the local/VPC boundary. Every routing decision (task, target, model, sensitivity, reason) is written to the audit trail.

The router (`tvastr.llm.router.HybridRouter`) is the single choke point for this policy. Backends sit behind a common `LLMClient` protocol so mock and real clients are interchangeable. Recorded in ADR-0002.

## 6. Why LlamaIndex specifically — bug patterns the agent should handle

LlamaIndex is a productive testbed because its real failures cluster into a handful of recurring shapes the agent can plausibly fix. A non-exhaustive list:

| Pattern | What it looks like | Why the agent can fix it |
| --- | --- | --- |
| **Deprecated import (post-v0.10 split)** | `ModuleNotFoundError: No module named 'llama_index.llms.openai'` or legacy `from llama_index import OpenAI` failing | Mechanical: rewrite import to `llama_index.llms.openai` after the namespace split. |
| **`ServiceContext` → `Settings` migration** | `DeprecationWarning: ServiceContext is deprecated, use Settings` | Mechanical: swap `ServiceContext.from_defaults(...)` for `Settings.llm = ...; Settings.embed_model = ...`. |
| **Schema / type mismatch between nodes** | `ValueError: Unexpected type 'TextNode' for field 'documents'`, `pydantic.ValidationError` | Trace to the connecting code, align producer/consumer types. |
| **Vector-store embedding dimension mismatch** | `ValueError: Embedding dimension 1536 does not match collection dimension 768` | Detect mismatch, recommend re-indexing with the correct embed model or updating the collection. |
| **LLM provider rate limit / context-window overflow** | `openai.RateLimitError`, `BadRequestError: This model's maximum context length is N tokens` | Add retry/backoff config, or chunk-size guidance. |
| **Async/sync mixing** | `RuntimeError: This event loop is already running` when calling `query_engine.query` inside an async context | Suggest the `aquery` variant or a `nest_asyncio` workaround. |
| **Agent JSON parsing failures** | `pydantic.ValidationError` parsing tool-call output from a weaker LLM | Tighten the agent's output parser or add a retry-with-correction. |
| **Callback handler errors** | Trace from `CallbackManager.on_event_start` | Usually a missing handler argument or a custom handler not following the protocol. |

The agent doesn't need to fix every category to be valuable — it needs to handle the *recurring* ones (the deprecated-import and `ServiceContext` migrations alone account for a meaningful share of the issue tracker right after the v0.10 split).

LlamaIndex's namespace structure also gives the code-retrieval layer something to chew on: the post-v0.10 split spreads the codebase across `llama-index-core`, `llama-index-llms-*`, `llama-index-vector-stores-*`, `llama-index-embeddings-*`, etc. The code-search tool needs to know to look in the right subpackage given an exception class — a non-trivial signal that real Haystack didn't exercise as cleanly.

## 7. Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| Orchestration | LangGraph + LangChain Core | First-class state machines; mature ReAct primitive; debuggable graphs. |
| Cloud LLM | Anthropic Claude (Opus / Sonnet) | Strongest code reasoning today; good structured output with prompting. |
| Local LLM | Ollama running Llama 3.1 / Mistral | Self-hosted, no per-call cost, runs on a developer laptop. |
| API surface | FastAPI + Uvicorn | Async, OpenAPI-by-default, fits the small-services style. |
| Compute | AWS ECS Fargate (long-running) + Lambda (event ingestion) | No nodes to manage, scales to zero. |
| Storage | OpenSearch | Already the log store; reusing it for the audit index avoids a second system. |
| IaC | Terraform (or CDK) | Standard, reviewable. |
| Observability | Langfuse (self-hosted) | LLM-aware tracing; cost + latency per call. |
| GitHub | PyGithub | Mature, covers everything needed (search, file read, branch, PR). |
| Notifications | Slack webhooks | One-way, simple, good enough. |

## 8. Roadmap — 2.5 months, Claude-Code-accelerated

The schedule assumes ~10 hours/week and aggressive use of Claude Code for scaffolding, tests, and docs.

**Weeks 1–2 — Foundation & MVP** *(in progress)*
- Project scaffolding, config, structured logging, domain models. ✅
- Ingestion (simulated source + GitHub-issues harvester). ✅
- Detection: fingerprint clustering, PII regex, threshold engine. ✅
- Agent skeleton: investigate → reason → generate fix → draft PR → open PR → notify. ✅
- Hybrid router with mock backends. ✅
- First end-to-end demo against bundled sample logs. ✅
- Dry-run mode + structured fix-generation with diff display. ✅
- ADR-0001 (record decisions), ADR-0002 (hybrid routing). ✅
- **Pivot the testbed to LlamaIndex:** replace bundled sample logs, default repo, mock paths, README copy. *(this turn)*

**Weeks 3–4 — Intelligence & hybrid routing**
- Multi-turn agent reasoning (retry fix gen on validation failure; ask for diff with more context).
- Local-model PII classifier (replace regex).
- Smarter code search: combine exception type + nouns from message; fall back to symbol search.
- Stack-trace extraction from GitHub issue bodies.
- Real Anthropic API integration validated against a real LlamaIndex issue (dry-run).
- Slack integration polish.
- Repo-ownership guardrail (`TVASTR_ALLOW_UPSTREAM`).

**Weeks 5–6 — AWS deployment & observability**
- Terraform / CDK for ECS Fargate + Lambda + EventBridge + SQS + OpenSearch.
- Dockerise the agent; CI builds & pushes images.
- Self-hosted Langfuse; instrument every LLM call.
- Dashboards: ingestion rate, pattern selection rate, PR-open success rate, dry-run output review queue.
- Load test: replay 1k synthetic events; confirm the threshold engine and dedup hold.

**Weeks 7–10 — Polish, docs, open-source launch**
- README that tells the story (problem, architecture, hybrid routing rationale, demo).
- 3-minute screencast: log arrives → pattern fires → agent opens a dry-run PR with a real diff against a LlamaIndex fork.
- Blog post: "I built an autonomous code remediation agent — here's what worked and what didn't."
- LinkedIn post + Show HN.
- Tag v1.0.

## 9. Migration notes (from Haystack version)

The architecture, the routing policy, the agent graph, the dry-run mode — none of these change. What changes is the testbed wiring:

| Change | File | Action |
| --- | --- | --- |
| Default target repo | `.env.example` (`TVASTR_GITHUB_REPO`), `src/tvastr/config.py` default | `deepset-ai/haystack` → `run-llama/llama_index` |
| Bundled sample logs | `data/sample_logs/haystack_failures.jsonl` | New `data/sample_logs/llamaindex_failures.jsonl` with the bug patterns from §6 |
| Mock GitHub paths | `src/tvastr/integrations/github.py` (`MockGitHubClient`) | Path prefixes change from `haystack/...` → `llama_index/...` (or the appropriate `llama-index-*` subpackage) |
| README copy | `README.md` | Replace Haystack-specific demo prose with LlamaIndex |
| Project memory | `MEMORY.md` and project-tvastr memory | Update "Test bed" to LlamaIndex |
| Old design PDF | `docs/design-doc.pdf` | Mark as "Haystack-era, superseded by `design-doc.md`" or delete |
| ADR | `docs/adr/` | Add ADR-0003 *"Switch testbed from Haystack to LlamaIndex"* documenting the why (`run-llama/llama_index` better matches the schema-validation / deprecated-import bug shape the agent is good at) |

None of this changes the test suite — tests are testbed-agnostic. The bundled sample logs are a fixture, not a coupling.

## 10. Risks & open questions

- **Code search precision.** The current `search_codebase(exception_type)` returns weak hits in real GitHub. For LlamaIndex's split namespace, this is harder, not easier. Needs prompt-engineered multi-step search or a learned ranker.
- **Fix quality without stack traces.** GitHub-issue-sourced events lack tracebacks unless the body contains one verbatim. Stack-trace extraction from issue bodies is the next high-leverage feature.
- **Upstream PR etiquette.** If/when we ever target upstream `run-llama/llama_index`, opening a flurry of agent-written PRs would be hostile to maintainers. The `draft=True` + repo-ownership guardrail is the technical defence; clear documentation that this is a **fork-only** tool by default is the social one.
- **Claude reliability for structured JSON.** Today the system prompt asks for JSON-only; in practice Claude usually complies, but the parser must handle leading prose / markdown fences. Migrating to the Anthropic SDK's tool-use feature would be more robust.
- **Cost.** Claude calls dominate per-pattern cost. A cheap-model first-pass (Haiku) with escalation to Opus on low confidence would cut spend ~5×.

## 11. References

- ADR-0001 — Record architecture decisions
- ADR-0002 — Hybrid local/cloud LLM routing
- ADR-0003 *(planned)* — Switch testbed from Haystack to LlamaIndex
- `docs/design-doc.pdf` — original (Haystack-era) design doc, superseded by this file
