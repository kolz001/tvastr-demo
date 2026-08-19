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
make test                # run the test suite (365 passed, 1 skipped, <30s)
make run                 # start the server  (visit /app for the triage UI, /docs for OpenAPI)
```

**Or run it as a container:** `docker compose up` builds the image, mounts
`./data` and the host's docker socket (the verify sandbox needs it), and
restarts on crash — visit `http://localhost:8001/app` (compose maps host
8001 → container 8000). CI (`.github/workflows/ci.yml`) builds that same
image on every push and drives a mock run through the job API to
`pipeline.end` — a smoke test that the *container* works, not just the
source tree.

**Optional — local NER for PII:** the boundary redactor runs on a deterministic
regex floor out of the box. To also catch unstructured PII (names, locations,
orgs) with a local model, install the `pii` extra and a spaCy model, then set
`TVASTR_PII_LOCAL_MODEL=true`:

```bash
uv sync --extra pii
uv run python -m spacy download en_core_web_lg
```

It's strictly additive and fail-open: the regex floor still applies, and any
model/dependency issue silently falls back to regex-only — redaction never does
*less* than the floor guarantees. Detection stays entirely on-device.

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
- **A run story card** at the top of the pane: five rows — Issue, Diagnosis,
  Fix, Verified?, vs. human fix — filling in as the run progresses, each with
  a plain-English gloss (`verified_via_warning` reads as "the fix didn't
  change behavior, but it now warns instead of silently misbehaving"). Click
  a row to jump to the timeline card that produced it. The raw event stream
  itself is split by **chapter dividers** into five plain-English phases
  (reading the failure → investigating → writing the fix → comparing to the
  human fix → proving it in a sandbox), each sealed on its first match.
- **A job model, not a blocking request.** `POST /api/run` returns a `run_id`
  immediately; the UI attaches to `GET /api/runs/{run_id}/stream`, which
  replays what's persisted so far and then tails new events — refresh
  mid-run and it re-attaches with no gap. A server restart mid-run gets
  swept on the next startup and honestly marked `pipeline.interrupted`,
  never left looking "in progress" forever.
- **Persisted runs.** Each run is written to `data/runs/<run_id>.jsonl`. The
  *Past runs* tab lists every previous run; clicking one replays the event
  stream through the same endpoint. Demos are reproducible — link a `run_id`
  and anyone can re-watch the same agent run later.
- **Auto-detect mode.** Live (real GitHub + real Claude) if `ANTHROPIC_API_KEY`
  and `GITHUB_TOKEN` are set; mock otherwise. The header badge shows which.
- **Dry-run by default.** The UI never opens a real PR. Use the CLI
  (`tvastr demo`) when you're ready to flip the safety off.

The architecture and trade-offs are written up in
[ADR-0004](docs/adr/0004-live-pipeline-instrumentation-via-event-sinks.md),
[ADR-0008](docs/adr/0008-job-model-run-lifecycle.md), and
[docs/design-doc.md §4.6](docs/design-doc.md).

### The agent verifies its own fixes

A diff isn't a fix — the loop closes only when we've shown the failure goes
away, on the agent's own patch, not a lucky sandbox. After the agent produces
a `pr.dry_run` in the triage UI, a **"Verify this fix"** button appears on the
timeline. Click it and tvastr runs the full chain:

1. **Synthesize a reproducer, behavioral-preferred.** Extract a runnable
   Python block from the issue body when one exists; otherwise ask Claude for
   one, tagged `behavioral` (a real round-trip postcondition — the value
   comes back *correct*, not just "no exception") or `crash` (the original
   exception is gone). A self-critique pass adversarially asks "would a
   suppress-only fix still pass this?" and rewrites toward a real assertion
   if so — skipped for fixes whose job is only to *warn* or *raise a
   clearer error* (register-aware, below), since those assertions are
   intentionally non-behavioral.
2. **Provision the sandbox's dependencies.** LlamaIndex is hundreds of
   separately-installable integration packages; the base image ships only a
   handful. tvastr `pip install`s the issue's actual integration into an
   isolated target directory first, so an issue on (say) the Postgres
   vector store doesn't fail with an honest but uninformative `repro_broken`
   just because Postgres isn't in the base image.
3. **Read code as of the issue, and overlay the buggy source if needed.**
   Baseline and overlay reads resolve against the commit at issue-filing
   time, not `main` — files get renamed or deleted upstream. And when a
   linked PR is known, tvastr overlays source-at-the-PR's-buggy-parent for
   the files that PR touched, because provisioning installs the *released*
   wheel — if the fix has since shipped, the wheel already contains it and
   baseline never fails (`no_repro`, honest but useless). Human-PR files the
   agent's own fix doesn't touch stay buggy through the agent's rerun, so
   the rerun tests the agent's fix alone.
4. **Apply the patch on the actual import path.** The reproducer imports the
   pip-installed package; the patch is bootstrapped into the same container
   before that import resolves, so a passing reproducer genuinely exercised
   the agent's diff, not the pristine installed copy.
5. **Baseline → patch → re-run**, optionally against a scoped slice of the
   project's own tests as a regression check.
6. **Repair a broken reproducer, bounded.** If the reproducer itself won't
   run (`repro_broken` — a real import error unrelated to the bug under
   test), retry up to twice, each attempt in a **fresh sandbox** (an earlier
   version leaked a patch across attempts — now a regression test) — before
   giving up.
7. **Emit a verdict**, honest about which oracle produced it:

| Verdict | Meaning |
| --- | --- |
| `verified_via_reproducer` | Repro no longer raises + exit 0. Strongest. |
| `verified_via_scoped_tests` | Repro ambiguous; scoped tests all pass. |
| `verified_via_behavior` | Behavioral repro's round-trip postcondition holds. |
| `verified_via_warning` | Fix's job was to warn, not change behavior — it now warns. |
| `verified_via_better_error` | Fix's job was a clearer error — it now raises one. |
| `masks_symptom` | Repro passes, but the assertion looks suppress-only. |
| `unverified_smoke_import_only` | Patch applied; no clear signal either way. Honest. |
| `unverified_doc_only` | Fix only touches docs/comments — nothing to execute. |
| `repro_broken` | Reproducer itself won't run, even after repair attempts. |
| `no_repro` | Baseline didn't trigger the bug — can't evaluate the fix. |
| `still_broken` | Repro post-patch still raises. |
| `regression` | Repro passes but scoped tests fail. |
| `environmental_error` | Sandbox or repro synthesis failed. |

The verdict appears in the **Past runs** table's `Verified?` column, with the
same plain-English gloss the story card uses, so a run's outcome is legible
at a glance later. See [ADR-0005](docs/adr/0005-verify-fix-loop.md) for the
original design and [ADR-0007](docs/adr/0007-verify-oracle-hardening.md) for
everything layered on top since; [docs/design-doc.md
§4.7](docs/design-doc.md) has the full loop.

**Honest limits worth knowing**

- Docker's sandbox flags are load-bearing for synthesized Python; patch
  bootstrapping is the one deliberate, scoped-to-a-single-write relaxation of
  `--read-only`. The subprocess fallback is weaker across the board — use it
  for development, not for verifying patches from untrusted sources.
- `no_repro` is still common with a thin issue body and no linked PR to
  overlay against; an LLM-authored oracle can itself be wrong, which is
  exactly why `masks_symptom` exists as its own verdict rather than a false
  green.
- Verify trusts a fix's declared register (repair / warn / better-error /
  document) instead of independently re-classifying it — a mislabeled
  register is graded by the wrong oracle today.
- Each verification is a few Claude calls (~$0.02–0.10) plus tens of seconds
  of sandbox time; the UI shows the cost before firing.

### Benchmarking against the human fix

When an issue has a linked pull request, `compare_to_pr` grades the agent's
fix against the one a maintainer actually shipped: a `match` / `partial` /
`divergent` verdict plus a `same_root_cause` flag and which files each side
touched. It's independent of verification — verify asks "does the failure go
away," this asks "did we fix the *same thing*, the way the project did."

The comparison is itself judged by an LLM, which doesn't reliably stay inside
that three-value enum — it was observed answering with `equivalent`, `weak`,
`different`, and other free-form vocabulary in roughly 39 of 44 persisted
comparisons. The original code treated anything off-enum as the worst bucket
(`divergent`), silently penalizing fixes the judge actually considered
equivalent. The fix normalizes the judge's output **by meaning** before it's
persisted, so "equivalent" is scored as `match`, not `divergent`.

**Honest limit:** the reference PR is itself just a human fix, and human
fixes are sometimes wrong, incomplete, or address a different symptom than
the one the agent found — a `divergent` verdict is evidence worth reading,
not a ground-truth failing grade.

To use Docker, build the LlamaIndex base image once:

```bash
docker build -t tvastr-verify:llamaindex -f verification/Dockerfile.llamaindex .
```

### The agent watches itself

tvastr's own failures — unhandled exceptions, upstream GitHub errors,
confidence-0.0 investigations, broken reproducers — used to vanish into
stdout or sit unread in `data/runs/`. The self-healing loop makes tvastr its
own first customer: it dogfoods the same diagnose/fix/verify pipeline it runs
against the target repo, against its own source.

1. **Capture (continuous).** With `TVASTR_SELF_HEAL_ENABLED=true`, every log
   record is mirrored to a dated JSONL file under `data/selflogs/`, alongside
   — never instead of — the normal console/JSON output.
2. **Daily digest.** `data/selflogs/` plus the day's `data/runs/*.jsonl` are
   mined for ops signals (errors, mapped GitHub failures) and quality signals
   (confidence-0.0 investigations, broken reproducers) and clustered by the
   existing `FailureDetector` — the same fingerprinting used everywhere else.
3. **Weekly top-10.** A week's daily digests are merged and ranked by
   `count × severity weight`: quality signals outrank ops noise, known/handled
   upstream errors (mapped 502s, rate limits) are dampened, and clusters that
   match a deliberate refusal (e.g. "no error signature found") are dampened
   further and permanently barred from the fix wave — remediating intended
   behavior is meaningless work. The top 10 land in the weekly report; the
   top 3 become fix candidates.
4. **Self-remediation + Slack escalation.** Each fix candidate runs through
   the *same* pipeline used for the target repo, pointed at tvastr's own
   repo. A verified fix becomes a branch + PR — never auto-merged. One
   consolidated Slack message per week reports fixed / attempted-but-
   unverified / skipped / report-only clusters, each tagged with a short
   fingerprint for follow-up.

**Two-switch PR safety.** `self_heal_enabled=true` alone never opens a real
PR: the fix wave forces `dry_run=True` unless `self_heal_open_prs=true` is
*also* set explicitly — enabling the loop and letting it open live PRs are
deliberately separate switches.

**Demo without waiting a week.** `POST /api/selfheal/scan {"kind": "daily"}`
(or `"weekly"`) runs one stage synchronously and works regardless of
`self_heal_enabled` — only the background scheduler is gated by that flag,
and a manual scan never runs the fix wave, so it can never open a PR. The
dashboard's **Self-heal** tab has a "Run digest now" button wired to it, plus
the latest weekly report (rank, count, status, PR links).

**Config** (all `TVASTR_*`-overridable):

| field | default | meaning |
| --- | --- | --- |
| `self_heal_enabled` | `false` | master switch: capture + scheduler |
| `self_heal_repo` | `kolz001/tvastr-demo` | remediation target repo |
| `self_heal_daily_hour` | `2` | UTC hour for the daily digest |
| `self_heal_weekly_day` | `7` | isoweekday for consolidation (7 = Sunday) |
| `self_heal_top_n` | `10` | clusters in the weekly report |
| `self_heal_fix_n` | `3` | clusters sent through the pipeline |
| `self_heal_retention_days` | `30` | selflog retention |
| `self_heal_open_prs` | `false` | opt-in: let the fix wave open real PRs |

Slack reuses `TVASTR_SLACK_WEBHOOK_URL` — no new secrets. See
[ADR-0009](docs/adr/0009-self-healing-loop.md) and [docs/design-doc.md
§4.9](docs/design-doc.md) for the full design.

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

**2. From a Grafana Loki cluster** (OSS, self-hostable) — `TVASTR_LOKI_URL=... TVASTR_LOKI_QUERY='{app="myapp"} |= "Error"' uv run python -m tvastr.cli ingest-loki --out data/sample_logs/loki.jsonl`, then `demo --logs data/sample_logs/loki.jsonl --dry-run`.

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
├── events.py          # PipelineEvent + EventSink protocol (Null/List/Jsonl/Fanout); run sweep
├── domain/            # pydantic models shared across layers
├── ingestion/         # log sources: simulated, stdin, github_issues, loki      ── INGESTION
├── detection/         # clustering, detector, threshold engine                   ── DETECTION
├── pii/               # PII / secret redaction at the boundary
├── llm/               # local + cloud clients, hybrid router
├── agent/             # investigator, graph, tools, retrieval/, sdk_schema.py    ── REASONING
├── analysis/          # pr_discovery, pr_analysis, fix_comparison               ── BENCHMARK
├── integrations/      # GitHub, Slack (real + mock + dry-run decorator)
├── storage/           # audit stores: in-memory, file (default), OpenSearch      ── OUTPUT
├── pipeline.py        # end-to-end orchestration (event-sink instrumented)
├── verification/      # sandbox + reproducer + verifier + repair loop           ── VERIFY
├── api/
│   ├── app.py         # FastAPI factory
│   ├── routes/        # health, issues, pr, remediate, run (job API), verify
│   └── templates/
│       └── app.html   # single-file triage UI: story card, chapter dividers
└── cli.py             # tvastr demo|serve|ingest-github-issues|ingest-loki|version

scripts/ci-smoke.sh        # drives a mock run through the job API in CI
Dockerfile                 # multi-stage uv build; runtime stage adds the docker CLI
docker-compose.yml         # tvastr + OpenSearch + Dashboards, socket-mounted sandbox
.github/workflows/ci.yml   # offline test suite, then a built-image smoke test
```

Every integration ships a real client and a mock behind a shared interface; the
`build_*` factories choose based on `Settings`, so the system depends on behavior,
not wiring. Every package above that prompts an LLM (`agent`, `analysis`,
`verification`) keeps its own `prompts.py` next to the code that uses it,
rather than one shared module.

## Roadmap

Full architecture and timeline: [docs/design-doc.md](docs/design-doc.md).

- **Weeks 1-2** — Foundation & MVP: pipeline, detector, LangGraph agent,
  dry-run mode, structured fix generation, ADRs. *Done.*
- **Weeks 3-4** — Intelligence & hybrid routing: *mostly done, deeper than
  planned.* Diagnosis was rewritten from a single-shot reason/expand pair
  into an agentic investigator (bounded tool loop, issue-era code reads,
  SDK-schema grounding); verification grew dependency provisioning, a
  buggy-file overlay, register-aware oracles, and a repair loop; a
  benchmarking pass grades every fix against its linked human PR. Still open:
  a local-model PII classifier (regex floor still carries production
  traffic) and Slack polish.
- **Weeks 5-6** — Containers landed ahead of schedule (Dockerfile, `docker
  compose up`, GitHub Actions CI with an offline suite + built-image smoke
  test) — local/CI infrastructure, not the AWS deployment this slot
  originally described. Terraform/CDK, ECS Fargate, EventBridge→SQS→Lambda,
  Langfuse traces, and a load test are still ahead.
- **Weeks 7-10** — Polish, docs, open-source launch. This refresh is part of
  that; the screencast, blog post, and v1.0 tag are still ahead.

## Tech stack

LangGraph · Claude API · Ollama · FastAPI · OpenSearch · AWS (ECS Fargate + Lambda) ·
Terraform/CDK · Langfuse · PyGithub · Slack webhooks.

## License

Apache-2.0.
