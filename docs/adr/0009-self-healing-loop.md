# 9. The self-healing loop: tvastr dogfoods its own remediation pipeline

- Status: Accepted
- Date: 2026-08-19

## Context

tvastr diagnoses and fixes failures in *other* repositories — that is the
whole product. But tvastr itself fails too: unhandled exceptions, GitHub
upstream errors (mapped to explained 502s or not), confidence-0.0
investigations, verify verdicts of `REPRO_BROKEN`. None of that is captured
anywhere durable. It either scrolls past in stdout or sits, unread, in
`data/runs/*.jsonl` alongside every other run — indistinguishable from the
target-repo runs it was built to observe. The agent that remediates code
cannot see itself degrade.

## Decision

**tvastr becomes its own first customer.** A new package
(`src/tvastr/selfheal/`), gated end-to-end by `self_heal_enabled` (default
`False`), captures tvastr's own logs continuously, digests them daily into
fingerprinted issue clusters using the *existing* `FailureDetector` — no new
clustering logic — consolidates weekly into a ranked top-10 report, runs the
top-ranked clusters through tvastr's own remediation pipeline targeting
tvastr's own repository, and escalates whatever it cannot fix via Slack.
Four stages:

1. **Selflog capture** — a structlog processor (`SelfLogWriter`) mirrors
   every log record to `data/selflogs/tvastr-YYYY-MM-DD.jsonl`, alongside
   the existing console/JSON output, best-effort and swallowing any write
   failure after one warning so capture can never destabilize the app it
   watches.
2. **Daily digest** (`selfheal/scan.py`) — mines one day's selflogs plus
   that day's run files for ops signals (errors) and quality signals
   (confidence 0.0, `REPRO_BROKEN`, failed `llm.call`), normalizes them
   into `LogEvent`s, and clusters them with the same `FailureDetector` used
   for target-repo failures.
3. **Weekly consolidation** (`selfheal/report.py`) — merges a week of daily
   digests by fingerprint, scores each cluster `count × severity_weight`,
   and splits the top `self_heal_top_n` (default 10) into `to_fix` (top
   `self_heal_fix_n`, default 3) and `report_only`.
4. **Self-remediation + escalation** (`selfheal/remediate.py`) — each
   `to_fix` cluster runs through the *same* `RemediationPipeline` the API
   route uses, pointed at `self_heal_repo` instead of the target repo, one
   run at a time (never concurrent — every self-fix touches the same repo).
   A verified fix becomes a branch + PR; one consolidated Slack message
   reports fixed / attempted-but-unverified / skipped / report-only
   clusters, each carrying a fingerprint token.

**Two guards make the loop safe to run unattended:**

- **Loop exclusion.** `pipeline.py` spreads a run's `run_meta` flat into the
  `pipeline.start` event's payload; every self-heal-launched run is tagged
  `run_meta = {"self_heal": True, ...}`. `scan.py`'s daily digest skips the
  *entire* run file whenever any event's payload carries a truthy
  `self_heal` key, and skips any selflog record logged from a self-heal
  context (`structlog.contextvars.bind_contextvars(self_heal=True)`, bound
  inside the wave's own run thread since contextvars don't cross thread
  boundaries). Both closures matter: without the run-file guard, a crashing
  self-run's `error` event would be mined as a fresh failure next digest;
  without the selflog guard, an ordinary log line emitted mid-run would leak
  through unmarked even though the run itself is excluded. This is a hard
  requirement, not a tuning knob — without it, one crashing self-run feeds
  itself forever.
- **Single scheduler.** `selfheal/scheduler.py` runs a daemon thread started
  by `create_app()` only when `self_heal_enabled` is true — the same
  posture as the existing startup sweep (`sweep_on_startup`): sealed false
  in `tests/conftest.py`, false in `docker-compose.yml` because the host
  dev process and the container share `./data`, and there must be exactly
  one scheduler firing against it. A state file
  (`data/selfheal/state.json`) makes firing idempotent across restarts —
  state is saved *before* the hook runs, so a crashing job never re-fires,
  and a missed period is caught up exactly once (yesterday for the daily
  job; the last complete ISO week for the weekly job) rather than replayed.

**Two-switch PR safety.** `self_heal_enabled=true` alone never opens a real
PR. The fix wave (`_self_run_settings`) forces `dry_run=True` for every
self-remediation run unless `self_heal_open_prs` (a config field this ADR
adds; default `False`) is explicitly set. Enabling the loop and letting it
push live PRs against `self_heal_repo` are deliberately separate switches,
because the wave runs autonomously — no human picks the cluster the way the
triage UI's single-issue mode does.

**The ranking bar: `by_design`.** `SEVERITY_WEIGHTS` in `report.py` started
with three classes (`quality` 3.0, `ops` 1.0, `handled_upstream` 0.25 for
known/explained upstream failures like mapped 502s). A fourth class,
`by_design` (0.05), was added after the first real weekly report
(2026-W27): 234 occurrences of a deliberate refusal message ("no error
signature found in issue title or body" — the agent correctly declining to
remediate a feature request with no traceable signature) scored 234.0 under
the original three-class weighting and outranked every other cluster,
including a genuine confidence-0.0 quality signal that scored only 6.0.
`by_design` clusters stay visible in the report (transparency), but are
**permanently barred from `to_fix`** regardless of score or rank — a fix
wave cannot meaningfully "fix" intended behavior. Post-tune, the
confidence-0.0 signal fills `to_fix` as intended.

**Manual demo path.** `POST /api/selfheal/scan` runs one stage (`daily` or
`weekly`) synchronously and is deliberately allowed regardless of
`self_heal_enabled` — only the background *scheduler* is gated by that
flag, so the loop is demoable without waiting a week or flipping the
feature on. It never runs the fix wave, so a manual scan can never open a
PR.

## Why these choices

- **Reuse the existing detector, not a parallel clustering path.** tvastr's
  own failures are the same *shape* of problem the product already solves —
  a recurring signal that needs fingerprinting and thresholding. Building a
  second clustering system for self-observed failures would duplicate
  `FailureDetector` for no benefit and risk the two diverging silently.
- **Reuse the existing pipeline, not a special-cased self-fix path.**
  Running self-remediation through `RemediationPipeline` (the identical
  code path the API route drives) means self-runs stream into the UI like
  any other run, get swept and interrupted the same way, and share the
  `IN_FLIGHT` registry (`tvastr.runner`) — there is no second run lifecycle
  to keep correct.
- **Two independent guards, not one.** The loop-exclusion guard stops the
  digest from mining a self-run's own events; the single-scheduler guard
  stops two processes from firing the same job twice. Neither substitutes
  for the other — a correct loop guard with two live schedulers would still
  double-fire, and a single scheduler with no loop guard would still feed on
  itself.
- **Two PR switches, not one.** `self_heal_enabled` answers "should tvastr
  watch and diagnose itself"; `self_heal_open_prs` answers "should it be
  allowed to push code changes to its own repo without a human choosing the
  issue." Collapsing these into one flag would mean the first time someone
  turns on self-observation in a live environment, they've also — possibly
  unknowingly — authorized autonomous PRs against their own source.
- **`by_design` as a hard bar, not just a low weight.** A low weight alone
  still lets a `by_design` cluster win `to_fix` if it recurs often enough
  relative to everything else that week; the W27 report is direct evidence
  that "often enough" is a real, not hypothetical, scenario. Barring it
  outright removes the failure mode instead of merely making it rarer.

## Consequences

- **First-report noise is real, not just a risk noted in the design.** The
  2026-W27 report is the evidence: a known, already-handled pattern (a
  by-design refusal) dominated the raw ranking before the `by_design` class
  existed. Any severity-weight tuning is inherently reactive — it requires
  a real digest to tune against, which means the first weeks of any fresh
  deployment will need a similar pass before `to_fix` reflects genuine
  signal.
- **Quality-signal attribution has a real ceiling.** A confidence-0.0 run
  may be caused by a thin or ambiguous target-repo issue, not by a tvastr
  defect — the digest records provenance (run id, issue reference) so a
  human reading the report can tell, but the ranking itself cannot. Some
  `to_fix` attempts will correctly conclude "not our bug" and produce no
  PR; that is a working outcome, not a failure of the loop.
- **Static ceiling.** The pipeline fixes *code*. A cluster rooted in
  configuration, environment, or an upstream service outage is escalation
  material, not remediation material — the Slack path is a first-class
  outcome of the loop, not a fallback for when remediation fails.
- **The scheduler thread blocks on the fix wave.** Because self-fix runs
  are sequential and the weekly hook runs them in-process on the scheduler
  thread, a slow or stuck fix wave delays the *next* tick's daily catch-up
  check until the wave finishes (bounded by `_POLL_SECONDS` re-evaluation,
  not blocked forever, but not concurrent with other self-heal work
  either).
- **`POST /api/selfheal/scan` is unauthenticated, file-writing work,
  reachable regardless of `self_heal_enabled`.** This matches the auth
  posture of the rest of the API today (no endpoint in this app is
  authenticated), and is recorded here rather than treated as a novel gap
  introduced by this feature — but it is real surface area: anyone who can
  reach the API can trigger a digest write.
- **A cluster's evidence is thin by construction.** Only up to three sample
  messages survive the digest → weekly-report round trip per cluster, so
  the self-remediation run investigates from a handful of representative
  log lines and timestamps, not the full event history that produced the
  cluster.

## Not yet exercised

The fix wave has run against mock data and unit/integration tests
throughout implementation, but a live wave against real GitHub — with
`self_heal_open_prs=true` and `TVASTR_USE_MOCKS=false` — has not yet been
exercised end-to-end at the time of this ADR. The first real wave will be
the first live test of the two-switch gate and the escalation path
together, watched interactively rather than run unattended.
