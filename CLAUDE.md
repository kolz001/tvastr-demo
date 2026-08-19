# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Everything runs through `uv` (the Makefile wraps it):

```sh
make install          # uv sync --extra dev — create venv, install all deps
make test             # uv run pytest (suite is fully offline — mocks, no network/keys needed)
make lint             # uv run ruff check src tests
make fmt              # ruff format + ruff check --fix
make typecheck        # uv run mypy
make demo             # end-to-end pipeline against sample logs, mock mode
make run              # uvicorn tvastr.api.app:create_app --factory --reload (port 8000)
```

Single test: `uv run pytest tests/test_agent_investigate.py::test_name`. Pytest is configured with `-q` and `pythonpath=src`; for an authoritative pass/fail count use `--junitxml` (the `-q` summary line is occasionally swallowed).

Docker: `docker compose up -d` starts OpenSearch **and** the containerized tvastr (host port 8001). CI smoke lives in `scripts/ci-smoke.sh` (drives the job API against mock issue 8001).

**Local machine convention:** the dev server runs detached on **port 8001** (`nohup uv run uvicorn tvastr.api.app:create_app --factory --port 8001`) because port 8000 belongs to an unrelated process (`my-bot`). No `--reload` — restart the server after merging.

## Architecture

tvastr is an autonomous code-remediation agent: it ingests failure signals (logs or GitHub issues), diagnoses the root cause in a target repo (testbed: `run-llama/llama_index`), generates a fix, verifies it in a sandbox, and benchmarks it against the human fix.

**`pipeline.py` is the seam where every layer meets.** `build_pipeline(Settings)` assembles the whole system; `RemediationPipeline.run` drives ingest → detect → threshold → agent → verify → audit.

- **Config (`config.py`)** — pydantic-settings, every field overridable via `TVASTR_*` env vars / `.env`. `use_mocks=True` (default) is the master switch: every integration gets an in-memory mock, so the whole app and test suite work with zero secrets. Feature flags (`doc_grounding`, `sdk_schema_grounding`, `issue_era_retrieval`, `verify_*`) gate each capability individually.
- **LLM routing (`llm/router.py`)** — hybrid local/cloud: a `TaskType` enum maps each call to Ollama (local; log parsing, summarization) or Claude (cloud; reasoning, fix generation, judging). **PII redaction (`pii/`) is the hard boundary before any cloud call** — raw log content never leaves the machine unredacted.
- **Agent (`agent/graph.py`)** — an agentic investigator: bounded JSON tool loop (`search`/`read_file`/`list_dir`, `_MAX_INVESTIGATE_ROUNDS = 4`), round 1 free-seeded from stack-trace files. On budget exhaustion a `for`/`else` forces one tool-less final-synthesis call so accumulated evidence is never discarded. Grounding then cross-checks the diagnosis via web search and **SDK-schema grounding** (`agent/sdk_schema.py`: wheels-only, no-deps `pip install --target data/sdk_cache/`, read as text only, never imported — LLM-supplied package names are validated as untrusted input). Code reads go through `IssueEraCodeHost`, pinned to the commit that existed when the issue was filed.
- **Verification (`verification/`)** — synthesizes a reproducer, runs baseline → apply fix → rerun in a sandbox (`sandbox.py`: docker or subprocess; dual-path work roots `TVASTR_SANDBOX_WORK_ROOT`/`_HOST_WORK_ROOT` for docker-out-of-docker). Register-aware oracles: a REPAIR fix must flip the reproducer; WARN/BETTER_ERROR/DOCUMENT fixes get different success criteria.
- **Benchmark (`analysis/fix_comparison.py`)** — judges the agent's fix against the linked human PR. Judge verdicts are normalized meaning-first (`_normalize`) because the model often answers off-enum; never silently worst-bucket. Known limitation: the reference PR can itself be wrong (see ADR 0006 postscript), so `divergent` is not ground truth.
- **Events (`events.py`)** — every pipeline moment is a `PipelineEvent` published to a pluggable `EventSink`; runs persist as JSONL under `data/runs/`. Terminality is `is_terminal_event` (`pipeline.end` / `pipeline.interrupted` / `error`), but verify events append *after* `pipeline.end` — any code checking run completeness must scan for ANY terminal event, not the last line. `mark_interrupted_runs` sweeps stale runs at startup, gated by `TVASTR_SWEEP_ON_STARTUP` (sealed false in `tests/conftest.py`; false in compose because host and container share `./data` and there must be only one sweeper).
- **API (`api/routes/`)** — job model: `POST /api/run` returns `202 {run_id}` immediately (the run file is pre-created before the worker thread starts); `GET /api/runs/{id}/stream` replays history then tails live, with a pre-read liveness snapshot and newline-bounded offsets. Upstream (GitHub) failures map to explained 502s, never bare 500s. The UI is a single vanilla-JS template, `api/templates/app.html`.

### Conventions

- **All LLM prompts live in per-package `prompts.py` files** (`agent/prompts.py`, `agent/tools/prompts.py`, `verification/prompts.py`, `analysis/prompts.py`) — never inline prompt strings in logic modules.
- **`docs/design-doc.md` is the authoritative design document** (the .pdf is superseded). ADRs in `docs/adr/` follow the Nygard convention and are **immutable** — supersede with a new ADR, never edit an accepted one.
- `data/` (`runs/`, `audit/`, `sdk_cache/`) is local state/cache, gitignored, never a versioned artifact — but real showcase runs live there on this machine, so be careful with anything that mutates it (see the sweep gate above).
- Mock fixtures for the offline suite: issue 8001 (has failure signature), 8050 (feature request) in `ingestion/github_issues.py`'s mock fetcher.
- Ruff line length 100, py312; mypy strict-ish (`disallow_untyped_defs`).
