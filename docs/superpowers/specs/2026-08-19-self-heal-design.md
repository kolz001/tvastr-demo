# Self-Healing Loop — Design

**Date:** 2026-08-19
**Author:** Nikhil (with Claude)
**Status:** Approved design — spec written from the approved two-section brainstorm

## Problem

tvastr diagnoses and fixes failures in *other* repositories, but its own
failures — unhandled exceptions, GitHub upstream errors, confidence-0.0
investigations, broken reproducers — vanish into stdout or sit unread in
`data/runs/`. The agent that remediates code cannot see itself degrade.

## Goal

tvastr becomes its own first customer: it captures its own logs continuously,
digests them daily into fingerprinted issue clusters, consolidates weekly into
a ranked top-10 report, runs the top K clusters through its **own remediation
pipeline** targeting its **own repository**, and escalates whatever it cannot
fix via Slack. Fixes land as pull requests on the tvastr repo — never merged
automatically.

**Success criterion:** with `self_heal_enabled=true` on the long-running host
process, a week of normal operation produces (a) dated selflog files, (b) daily
digests, (c) a weekly top-10 report visible in the UI, (d) at least one
self-remediation run whose events stream like any other run, and (e) a Slack
message listing the clusters that were not fixed. All of it works offline in
mock mode for tests and demos.

## Non-goals

- Auto-merging its own PRs (human review is the permanent gate).
- Real-time alerting (the loop is daily/weekly by design).
- Multi-process scheduling (one designated scheduler process, like the
  startup sweep).
- Fixing issues that originate outside the repo (infra, network, quota) —
  those are escalation material, not remediation material.

## Architecture

Four stages, two guards. New package: `src/tvastr/selfheal/`.

### Stage 1 — Selflog capture (continuous)

A structlog **processor** (`SelfLogWriter`) inserted into the shared chain
appends every log record as one JSON line to
`data/selflogs/tvastr-YYYY-MM-DD.jsonl` (UTC date), alongside — never instead
of — the existing console/JSON rendering. Date rollover prunes files older
than `self_heal_retention_days` (default 30). Thread-safe via a module lock;
any write failure is swallowed after one warning (capture must never break the
app). Enabled by `self_heal_enabled`.

The second signal source needs no new capture: `data/runs/*.jsonl` already
records every `error` event, LLM failure, verify verdict, and confidence.

### Stage 2 — Daily digest

`selfheal/scan.py` (pure functions) reads one UTC day's selflog file plus the
run files created that day and extracts **issue candidates**:

- **Ops signals:** selflog records with level `error`/`critical` (traceback
  text captured into `stack_trace`), and run events of type `error`.
- **Quality signals:** runs whose final root-cause confidence is 0.0, verify
  verdicts of `REPRO_BROKEN`, and `llm.call` events that record a failure.

Candidates are normalized into `LogEvent`s (`service="tvastr-self"`,
`source="selfheal"`, attributes carrying run_id/step provenance) and clustered
by the **existing `FailureDetector`** — no new fingerprinting. The digest
(clusters + counts + sample events) is written to
`data/selfheal/daily/YYYY-MM-DD.jsonl`.

### Stage 3 — Weekly consolidation

`selfheal/report.py` merges an ISO week's daily digests, re-ranks clusters by
`count × severity_weight` (quality signals outweigh ops noise; explained/
handled upstream errors like mapped 502s get a dampening weight), and writes
the top `self_heal_top_n` (default 10) to
`data/selfheal/weekly/YYYY-Www.json`, splitting them into `to_fix` (top
`self_heal_fix_n`, default 3) and `report_only`.

### Stage 4 — Self-remediation + escalation

For each `to_fix` cluster, a pipeline run is started through the existing
job-model machinery (`events` passed directly, threshold bypassed — same as
single-issue mode) with:

- `github_repo = self_heal_repo` (default `kolz001/tvastr-demo`), so the
  investigator reads and the fixer targets tvastr's own source;
- `run_meta.self_heal = true` (the loop-guard tag);
- normal event streaming, so self-runs are watchable in the UI like any run.

A verified fix becomes a branch + PR on the self repo via the existing PR
path. After the fix wave, one consolidated Slack message (existing
`build_notifier`) reports: fixed (with PR links), attempted-but-unverified,
and report-only clusters. In mock mode the MockSlackNotifier records it.

### Guard 1 — loop exclusion

Stage 2 **skips** any run whose `run_meta.self_heal` is true and any selflog
record logged from a self-heal-tagged context. Without this, one crashing
self-run feeds itself forever. This guard is a hard requirement, not a
tuning knob.

### Guard 2 — single scheduler

`selfheal/scheduler.py` runs a daemon thread started by `create_app()` only
when `self_heal_enabled` is true. Exactly like `sweep_on_startup`: sealed
false in `tests/conftest.py`, false in compose (host and container share
`./data`; the host uvicorn is the sole scheduler). A state file
(`data/selfheal/state.json`: `last_daily_date`, `last_weekly_week`) makes
firing idempotent across restarts — a missed day is caught up once at boot,
never replayed twice.

## Config (all `TVASTR_*`-overridable)

| field | default | meaning |
|---|---|---|
| `self_heal_enabled` | `False` | master switch: capture + scheduler |
| `self_heal_repo` | `"kolz001/tvastr-demo"` | remediation target repo |
| `self_heal_daily_hour` | `2` | UTC hour for the daily digest |
| `self_heal_weekly_day` | `6` | ISO weekday (6=Sunday) for consolidation |
| `self_heal_top_n` | `10` | clusters in the weekly report |
| `self_heal_fix_n` | `3` | clusters sent through the pipeline |
| `self_heal_retention_days` | `30` | selflog retention |
| `self_heal_open_prs` | `False` | wave opens real PRs only when explicitly enabled; otherwise forced dry-run |

Slack reuses `slack_webhook_url`. No new secrets besides the webhook.

## API / UI

- `GET /api/selfheal/status` — enabled flag, last/next daily and weekly fire
  times, latest report path, scheduler liveness.
- `GET /api/selfheal/report` — latest weekly report (404 until one exists).
- `POST /api/selfheal/scan` — manual trigger (daily digest for a given date,
  or weekly consolidation) so the loop is demoable without waiting a week.
- UI: a compact self-heal panel on the dashboard listing the latest report's
  clusters with rank, count, status (fixed / escalated / report-only) and PR
  links; self-runs open in the normal run view.

## Failure posture

Every stage degrades to "the app runs exactly as today": capture swallows
write errors, scan/report failures log and skip the day, scheduler thread
death is visible in `/api/selfheal/status` but never takes the app down, and
remediation failures are themselves ordinary run errors — which the next
digest will see. The loop observing its own failures is the point.

## Known risks

- **First-report noise:** early weeks will be dominated by known, already-
  handled patterns (mapped 502s, port-collision artifacts). Severity weights
  get one explicit tuning pass against real digest output before the feature
  is called done.
- **Quality-signal attribution:** a confidence-0.0 run may be caused by the
  target issue, not by a tvastr defect. The digest records provenance so the
  investigator (and the human reading the report) can tell; some escalations
  will be correctly closed as "not our bug".
- **Static ceiling:** the pipeline fixes code; a cluster rooted in config,
  environment, or upstream services is escalation material. The Slack path is
  a first-class outcome, not a failure mode.
