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

Sources are pulled (or pushed) into a uniform `LogEvent` shape (`service`, `severity`, `message`, `stack_trace`, `attributes`, `source`). The architecture is OSS-first and source-agnostic — `LogSource` is a Protocol, not a coupling to any vendor.

- **Universal escape hatch:** `StdinLogSource` reads JSONL of `LogEvent` from stdin. Anyone with logs in any system writes a small `jq`/`awk`/Python converter and pipes in — no adapter code needed.
- **`SimulatedLogSource`:** replays a bundled JSONL file. Default for local development and the demo.
- **`LokiLogFetcher`:** queries Grafana Loki (Apache-2.0, self-hostable) via its HTTP `/loki/api/v1/query_range` endpoint. `tvastr ingest-loki --query '{app="myapp"} |= "Error"'` writes a JSONL the simulated source replays. Default `line_to_event` converter handles JSON log lines; users with custom shapes pass a callable.
- **`GitHubIssuesFetcher`:** harvests bug-labelled issues from a target repo. Useful when you don't have a production log stream yet — the agent gets real-world signal from open-source bug trackers.
- **`CloudWatchLogSource`:** stub for the AWS production path (CloudWatch → EventBridge → SQS → Lambda → batch into the agent). Implemented in the AWS deployment phase.
- **Planned:** `OTLPLogSource` — OpenTelemetry Logs over OTLP/HTTP. The vendor-neutral CNCF spec; works with any OTel-compatible backend (Loki, Jaeger, Tempo, Honeycomb, SigNoz, etc.).

### 4.2 Detection

- **Fingerprint clustering.** Each event is normalised (UUIDs, hex, numbers, quoted literals stripped) and hashed with `service|exception_type|normalized_message`. The same failure with different runtime IDs collapses into one `FailurePattern`. Today's normaliser is regex-based and fast; a learned classifier is on the roadmap.
- **PII / sensitivity classification.** A regex scan over each event tags `Sensitivity.SENSITIVE` if it carries emails, API keys, credentialed URLs, or IPs. This is the input to the router (see §5). A local LLM classifier is the planned upgrade.
- **Threshold engine.** A pattern is escalated to the agent only when it recurs ≥ N times (default 3) within a dedup window. Patterns already handled (PR opened) are recorded so the agent doesn't re-fix what's already in flight.

### 4.3 Agentic reasoning

A LangGraph state machine. Each node is a focused step; tools are thin functions over an `AgentContext` (router, code host, notifier). The original single-shot `reason_root_cause` → `expand_context` pair (one cloud call, one bounded follow-up round) has been replaced by an agentic investigator, and a `ground_root_cause` step now sits between investigation and fix generation:

```
START
  └─→ investigate  ──(agentic tool loop over search / read_file / list_dir,
       │              ≤4 rounds; free-seeded from traceback + issue-body
       │              files; reads resolve at the issue-era commit; on
       │              budget exhaustion, one forced final-synthesis call
       │              over everything read rather than discarding it)
       └─→ confidence gate  (routing function, not a compiled node)
            ├─ act
            │    └─→ ground_root_cause  ──(web-search grounding, forced
            │         │                    tool_choice; + SDK-schema
            │         │                    evidence: probe → wheels-only
            │         │                    pip fetch → class-block
            │         │                    extraction → field-name
            │         │                    cross-check against the suspect
            │         │                    code's field reads)
            │         └─→ generate_fix  ──(register-aware FixProposal:
            │              │               REPAIR/FAIL_FAST/WARN/
            │              │               BETTER_ERROR/DOCUMENT)
            │              └─→ compare_to_pr  ──(benchmark vs. the issue's
            │                   │               linked human PR, if any)
            │                   └─→ draft_pr  ──→ open_pr  ──→ notify  ──→ END
            └─ skip ──────────────────────────────────────────────→ notify  ──→ END
```

**`investigate`** is an agentic tool loop (JSON-vocabulary propose→execute, not native tool-use): the model can call `search` (`search_codebase`), `read_file` (`retrieve_code_files`), or `list_dir` against the code host, up to `_MAX_INVESTIGATE_ROUNDS = 4` times. Round 1 is **free-seeded** — files named in the stack trace and in the issue body's own traceback text are fetched eagerly before the model spends a round asking for them. All reads go through an `IssueEraCodeHost` that resolves `get_file`/`list_dir` at the commit that existed when the issue was filed (falling back to `main` if the era-pinned read comes back empty) — issues are historical, and a file that's been renamed, merged, or deleted by the time the agent looks is not evidence that no bug exists there. If all 4 rounds elapse without the model emitting a `root_cause`, the original behavior discarded every file read and fell back to a hardcoded 0.0-confidence escalation; a `for`/`else` now fires exactly once on exhaustion and makes one additional tool-less LLM call over everything accumulated — including the last round's files, which the model's own JSON response never got to see — before falling back. This was a live gap, not a hypothetical one: on llama_index #19293, four rounds of one-file-at-a-time reads left the model one step from the answer when the budget ran out.

The **confidence gate** is a conditional-edge routing function (not a graph node) that compares the investigator's self-reported confidence against a threshold; below it, the run escalates to a human via Slack with the pattern + root-cause analysis rather than proceeding. This is the safety valve that keeps low-quality fixes off the PR queue.

**`ground_root_cause`** validates the investigator's story against external reality before any code gets written, in two layers. First, a forced web-search call (Anthropic's server-side `web_search` tool, with `tool_choice` pinned rather than left to the model's discretion — an undetermined tool choice was observed skipping the search nondeterministically across otherwise-identical runs). Second, **SDK-schema grounding**: a cheap structured call probes whether the bug plausibly involves a third-party SDK's response shape; if so, tvastr does a wheels-only, `--no-deps`, isolated-`--target` `pip install` of that package (nothing is ever imported or executed — only its `.py`/`.pyi` source is read as text), extracts the class definitions containing the probe's keywords, and prepends them to the grounding prompt as "ground truth for field names," with an explicit instruction to cross-check every field the suspect code reads against them. This exists because web search alone is subject to confirmation bias — a query built from a root cause the model already believes tends to return sources that confirm it, not challenge it. On #19293, the real bug was a third-party SDK returning token counts under a renamed field; doc-grounding's own search (built from the wrong story) had confirmed the wrong mechanism, and the fix was only reachable by reading the SDK's own type definitions. Note the ceiling here: schema evidence shows a field *exists* in a type definition, not that it's *populated* the way the agent assumes at runtime for the specific response the issue reported — grounding narrows the search space, it doesn't replace running the code.

The **fix generation** node asks Claude for *structured JSON* describing surgical `search`/`replace` operations against the retrieved source files, now carrying a `register` (see §4.7) that names what kind of fix it is, not just what it changes. Each operation is validated (search string must exist and be unique in the file), applied via `str.replace`, and rendered as a unified diff. If parsing or validation fails, the pipeline degrades to a placeholder rather than crash — the audit log captures the degradation.

**`compare_to_pr`** is new: when the issue has a linked pull request, `src/tvastr/analysis/` (`pr_discovery` → `pr_analysis` → `fix_comparison`) benchmarks the agent's fix against the human one before the PR is drafted — a `match`/`partial`/`divergent` verdict, a `same_root_cause` flag, and which files each side touched. This doesn't gate the pipeline (a `divergent` verdict is recorded, not blocked on), but it's the same-run, apples-to-apples signal a reviewer would otherwise have to construct by hand. The comparison is itself LLM-judged, and the judge doesn't reliably stay inside the three-value enum — roughly 39 of 44 persisted comparisons came back with off-enum vocabulary (`equivalent`, `weak`, `different`, ...), and the original code coerced every unrecognized string to the worst bucket (`divergent`), silently penalizing fixes the judge actually considered equivalent. `fix_comparison._normalize` now maps free-form vocabulary onto the schema **by meaning** before persisting. Honest limit: the reference PR is itself a human artifact and is sometimes wrong, incomplete, or aimed at a different symptom — `divergent` is evidence worth reading, not a ground-truth failing grade.

The full rationale for the investigator rewrite and grounding — including the #19293 case that motivated both — is in [ADR-0006](adr/0006-grounded-diagnosis.md).

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

### 4.6 Live instrumentation & triage UI

The agent's value is in *how* it decides, not just *that* it decided. The pipeline emits a `PipelineEvent` at every significant moment — node entry/exit, tool calls, LLM prompts and responses, routing decisions with the redacted payload, generated diffs, PR drafts, dry-run interceptions, audit writes. The emitter is a `EventSink` Protocol (`src/tvastr/events.py`) with four implementations:

- `NullEventSink` — the default. Zero overhead when instrumentation is off.
- `ListEventSink` — in-memory list for tests. Lets `tests/test_events.py` assert on the actual event sequence end-to-end instead of mocking structlog.
- `JsonlEventSink` — append-only JSONL writer. One file per run at `data/runs/<run_id>.jsonl`.
- `FanoutEventSink` — broadcasts to multiple sinks, swallowing per-sink errors so a misbehaving consumer (e.g. a disconnected SSE client) cannot break the pipeline.

The triage UI at `GET /app` surfaces this end-to-end. The run surface is a **job model**, not a blocking request — this was a deliberate rework (see [ADR-0008](adr/0008-job-model-run-lifecycle.md)) from an earlier version where `POST /api/run` held the connection open for the whole pipeline:

- `GET /api/issues?repo=&sort=reactions-+1|comments|interactions&label=&limit=` — ranked issue list. Live mode uses GitHub's Search API so the sort happens server-side; mock mode returns deterministic fixtures with synthetic reaction/comment counts. The same response shape and ordering contract in both.
- `POST /api/run {repo, issue_number}` — picks the issue, converts it to `LogEvent(s)` via `issue_to_events`, **bypasses the recurrence threshold** (the user has explicitly chosen this issue), pre-creates the run's JSONL file, and starts the pipeline in a background thread. Returns **202 `{"run_id": ...}` immediately** — the caller doesn't wait on the pipeline. Pre-creating the file (rather than letting the pipeline thread create it on first event) closes a race: a client that attaches to the stream a moment after the 202 could otherwise 404 a run that is, in fact, already running.
- `GET /api/runs/{run_id}/stream` — the single endpoint for **live attach, reattach, and pure replay**. It reads the persisted JSONL from the start (replay), then — if the run is still in-flight in this process — tails new lines as they're appended, and emits `event: done` once a terminal event has been seen or the pipeline thread has exited. A page refresh mid-run simply re-opens this endpoint and gets replay-then-tail again, with no gap and no separate "resume" concept. Offsets advance only on newline boundaries so a line caught mid-write is retried rather than emitted torn; liveness is snapshotted *before* the file is read, closing a race where the pipeline's last events could be missed between the liveness check and the read.
- `GET /api/runs` — past-runs index (newest first by mtime), built by scanning `data/runs/*.jsonl` and summarising each.
- `GET /api/runs/{run_id}` — full event stream of a past run, as JSON for replay or as SSE, independent of the live-tail endpoint above.
- `POST /api/runs/{run_id}/verify` — reconstructs the verifier's inputs from a persisted run and streams `verify.*` events, appending to the same JSONL (§4.7).

**Interrupted runs are swept honestly.** On startup (gated by `TVASTR_SWEEP_ON_STARTUP`, default on), `mark_interrupted_runs` scans `data/runs/*.jsonl` for runs with no terminal event and no longer in this process's in-flight registry, and appends a `pipeline.interrupted` event rather than leaving them looking permanently "in progress." The sweep originally judged terminality by the file's *last* event only — but the verify endpoint appends `verify.*` events onto the same file *after* `pipeline.end`, so a completed-and-verified run's last event is `verify.result`, not `pipeline.end`. That misjudged every verified run as interrupted on each sweep. The fix checks **any** event in the file for terminality, not just the last one. The in-flight registry is per-process, so a second process sweeping the same `data/` directory (e.g. a host dev server and a container both pointed at the same volume) would see the first process's live runs as not-in-flight and falsely mark them — `docker-compose.yml` sets `TVASTR_SWEEP_ON_STARTUP=false` for exactly this shared-data-host scenario; a pure single-process container deployment should leave it on.

The frontend (`src/tvastr/api/templates/app.html`) is a single self-contained file: vanilla JS, no build step, no framework. Triage tab on the left with the ranked issue list; live pipeline pane on the right that streams events as collapsible cards (one-line summary by default; full prompt/response/diff on click). Two additions sit above the raw event feed:

- **The run story card** — five rows (Issue, Diagnosis, Fix, Verified?, vs. human fix) derived 1:1 from the event stream, each filling in as its source event arrives and each carrying a plain-English gloss (`VERDICT_GLOSS`) rather than a bare enum value — e.g. `verified_via_warning` renders as "the fix didn't change behavior, but it now warns instead of silently misbehaving." Every row remembers the DOM element of the event that produced it; clicking the row scrolls to and expands that card, so every claim on the card is one click from its raw evidence.
- **Chapter dividers** — the event stream is grouped into five plain-English phases by event-type prefix (`ingest./detect./threshold.` → "Reading the failure"; `agent./tool./retrieval./doc./router./llm.` → "Investigating the cause"; `fix./pr.` → "Writing the fix"; `benchmark.` → "Comparing to the human fix"; `verify.` → "Proving it in a sandbox"). Each divider is inserted once, on its chapter's first matching event, and is then sealed — a later event whose prefix maps to an earlier chapter cannot reopen it, so out-of-order or repeated events (e.g. a second verification attempt) don't fragment the narrative.

Past-runs tab provides replay through the same stream endpoint above. The architecture-strip at the top of the pipeline pane lights up the active layer as the agent moves through it.

SSE was chosen over WebSocket for this surface because the stream is unidirectional, HTTP-native (no protocol upgrade, friendly to proxies), and consumable from vanilla `fetch()` without a client library. The user cancels by disconnecting.

The full rationale and trade-offs are in [ADR-0004](adr/0004-live-pipeline-instrumentation-via-event-sinks.md) (event sinks + SSE) and [ADR-0008](adr/0008-job-model-run-lifecycle.md) (job model, sweep, containers, CI).

### 4.7 Verification loop

A diff is not a fix. To make "the agent fixed it" mean something, every fix can be verified in a hermetic sandbox before being trusted. The chain (`src/tvastr/verification/`) has grown considerably since the original v1 design in [ADR-0005](adr/0005-verify-fix-loop.md); [ADR-0007](adr/0007-verify-oracle-hardening.md) covers the additions below.

**`FixRegister` first.** Before anything else, `Verifier.verify()` checks `fix.register`. `DOCUMENT`-register fixes (documentation/comment-only changes — nothing to execute) short-circuit immediately to `unverified_doc_only` with no sandbox at all. Every other register (`REPAIR`, `FAIL_FAST`, `WARN`, `BETTER_ERROR`) proceeds through the chain below, but the register changes what "success" means at the judging step — this is the resolution to a real tension: a maintainer-correct fix that only adds a `logger.warning` (register `WARN`) should never be graded `still_broken` by an oracle that only knows how to check "did the crash stop," because it was never supposed to change behavior. Verify is **register-polymorphic**, not register-blind: `WARN` maps to a `verified_via_warning` oracle (does the intended warning now fire), `BETTER_ERROR` maps to `verified_via_better_error` (does a clearer error now raise), and `REPAIR`/`FAIL_FAST` fall through to the default behavioral oracle. This is register-*aware*, not register-*verified* — verify trusts the label `generate_fix` attached rather than independently re-classifying it; a mislabeled register is graded by the wrong oracle today.

The chain, in the order the code runs it:

1. **Synthesize a reproducer once, before any sandbox exists — behavioral-preferred.** Extract a runnable block from the issue body when one parses cleanly; otherwise ask Claude for one. The response is tagged `behavioral` (asserts a real round-trip postcondition — the value comes back *correct*) or `crash` (asserts the original exception is gone). For `REPAIR`/`FAIL_FAST` behavioral repros, a second call (`REPRO_CRITIQUE`) adversarially asks "would a suppress-only fix still pass this?" and rewrites toward a genuine assertion if so; `WARN`/`BETTER_ERROR` repros skip this critique because their assertions (a `warnings.catch_warnings` check, or a `try/except` on error type/message) are intentionally non-behavioral, and the round-trip bias would fight them.
2. **Enter the repair loop** (up to `_MAX_REPRO_REPAIR = 2` repairs, i.e. 3 total attempts). Each attempt:
   a. **Prepare a fresh sandbox handle.** `DockerSandbox` (preferred) runs each command in a one-shot container with `--cap-drop=ALL --rm`; `--read-only`/`--network=none` hold except for the single scoped relaxation in step (d) below. `SubprocessSandbox` is the fallback when Docker isn't installed — weaker isolation, same contract.
   b. **Provision dependencies into this attempt's sandbox.** LlamaIndex is hundreds of separately-installable integration packages; the base image ships a fixed handful. tvastr derives the distribution(s) implied by the fix's changed files and `pip install --target`s them into an isolated directory prepended onto `PYTHONPATH`, so an issue on a long-tail integration doesn't fail purely because that integration isn't in the base image.
   c. **Baseline.** Run the reproducer pre-patch. If the original exception/behavior isn't observed, the sandbox lazily tries a **buggy-file overlay**: if a linked PR is known, fetch the PR-touched files at their pre-merge (buggy-parent) commit and overlay them, then retry the baseline. This exists because provisioning installs the *released wheel* — for a closed issue, that wheel usually already contains the fix, so a naive baseline reports `no_repro` (honest, but useless) even though the bug is real. If baseline still doesn't reproduce, verdict is `no_repro`.
   d. **Apply the patch on the actual import path.** The reproducer imports the pip-installed package, not a repo-relative path, so the patch is staged and bootstrapped inside the *same* container just before that import resolves (`sh -c "python apply.py && <repro>"`) — this is the one place `--read-only` is dropped, scoped to that single write. Overlay files the agent's own fix doesn't touch stay in their buggy state through this rerun, so the rerun tests the agent's fix alone rather than a mix of the agent's fix and the already-fixed upstream file.
   e. **Re-run**, then optionally run a scoped slice of the project's own tests (`TVASTR_VERIFY_PROJECT_ROOT`) as a regression check.
   f. **Triage.** If the verdict is `repro_broken` (the reproducer itself failed to execute, for a reason unrelated to the bug under test) and repair budget remains, discard this handle, **repair the reproducer** using what real dependency errors the attempt surfaced, and loop with a brand-new sandbox handle. An earlier version prepared and provisioned the sandbox once, outside the loop, and reused the same handle across repair attempts — attempt 2's baseline then ran against a handle still carrying attempt 1's patch, a silent cross-attempt leak. Each attempt now gets its own handle, discarded in a `finally` before the next one is prepared.
3. **Emit the verdict**, honest about which oracle produced it:

| Verdict | Meaning |
| --- | --- |
| `verified_via_reproducer` | Repro no longer raises + exit 0. Strongest. |
| `verified_via_scoped_tests` | Repro ambiguous; scoped tests all pass. |
| `verified_via_behavior` | Behavioral repro's round-trip postcondition holds. |
| `verified_via_warning` | Register `WARN`: the intended warning now fires. |
| `verified_via_better_error` | Register `BETTER_ERROR`: a clearer error now raises. |
| `masks_symptom` | Repro passes, but the assertion looks suppress-only. |
| `unverified_smoke_import_only` | Patch applied; no clear signal either way. Honest. |
| `unverified_doc_only` | `DOCUMENT`-register fix — nothing to execute. |
| `repro_broken` | Reproducer itself won't run, even after repair attempts. |
| `no_repro` | Baseline didn't trigger the bug — can't evaluate the fix. |
| `still_broken` | Repro post-patch still raises. |
| `regression` | Repro passes but scoped tests fail. |
| `environmental_error` | Sandbox or repro synthesis failed. |

Every step emits a `verify.*` event into the same `EventSink` the rest of the pipeline uses, so verification streams live into the UI and appends to the same `data/runs/<run_id>.jsonl` as the agent events. The Past-runs table and the run story card both show the verdict with the same plain-English gloss.

**Docker-out-of-docker: dual-path sandbox roots.** Once tvastr itself runs in a container with the host's `/var/run/docker.sock` mounted, a `docker run -v <path>` issued from inside that container is resolved by the *host* daemon, not by tvastr's own container filesystem — a path valid inside tvastr's container means nothing to the daemon on the other side of the socket. `Settings.sandbox_work_root` (where tvastr writes sandbox files, container-local) and `sandbox_host_work_root` (that same directory's path as the *host* sees it, used only in the `-v` flag) are now separate settings; when unset, the sandbox root is used for both, which is byte-identical to pre-change behavior. `docker-compose.yml` sets both so the containerized tvastr can still bind-mount its sandbox scratch space into the sibling containers it spins up on the host daemon.

**Honest limits.** `no_repro` is still common with a thin issue body and no linked PR to overlay against. An LLM-authored oracle can itself be wrong — a bad assertion can fail a genuinely correct fix, which is exactly why `masks_symptom` is its own verdict rather than a false green. Provisioning may shadow the base image's pinned dependency version with a different one; that's an acceptable trade for a verify sandbox, but it is not a no-op, and the provisioned version isn't guaranteed to match the version the issue reporter actually ran. A reproducer that needs a live service (e.g. an embedded database that downloads a server binary) is blocked by `--network=none`/`--read-only` by design.

In v1, verification is **user-triggered** from a "Verify this fix" button that appears on the live timeline after `pr.dry_run`. Once measured to be reliable, it moves onto the autonomous path between `generate_fix` and `draft_pr` (failure routes to `notify` rather than `draft_pr`). This deliberate two-step ship — described in [ADR-0005](adr/0005-verify-fix-loop.md) — lets us iterate on verifier reliability without silently degrading the agent.

### 4.8 Deployment & CI

tvastr ships as a container and is smoke-tested as one on every push ([ADR-0008](adr/0008-job-model-run-lifecycle.md)).

**`Dockerfile`** is a two-stage build. The build stage (`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`) resolves the locked environment with `uv sync --frozen`, split into a dependency-only sync before the source is copied and a second sync after, so dependency layers cache independently of source changes. The runtime stage (`python:3.12-slim-bookworm`) copies only the resolved `.venv` and `src/`, and additionally installs a **static Docker CLI binary** (client only, no daemon) so the running container can issue `docker run` for the verify sandbox against a socket mounted in from the host — this is the docker-out-of-docker setup §4.7 discusses. The container runs as root, a documented trade-off: the mounted host docker socket requires it for a local/portfolio deployment; a socket-proxy sidecar is the hardened alternative for anything more exposed.

**`docker-compose.yml`** runs the tvastr image alongside OpenSearch and OpenSearch Dashboards. It maps host port 8001 to the container's 8000 (8000 is commonly already taken by another local app), mounts `./data` for run persistence and `/var/run/docker.sock` for the verify sandbox, sets `restart: unless-stopped`, and sets both `sandbox_work_root`/`sandbox_host_work_root` for the dual-path mount described in §4.7. It also sets `TVASTR_SWEEP_ON_STARTUP=false` — on a developer machine where a host-run `uv run uvicorn` and this container might both point at the same `./data` directory, each process's sweep only knows about *its own* in-flight runs, so the second process to start would falsely mark the first's live runs as interrupted; a pure single-process container deployment should flip this back to `true`.

**CI** (`.github/workflows/ci.yml`) has two jobs. `test` runs the fully-offline suite (`uv sync --frozen`, `ruff check`, `pytest -q`) with no Docker involvement. `image`, gated on `test` passing, builds the actual `Dockerfile`, runs it with `TVASTR_USE_MOCKS=true`, and drives `scripts/ci-smoke.sh` against it — the script polls `/health`, `POST`s `/api/run` for a mock issue chosen because it has a real repro signature, then polls `GET /api/runs/{id}/stream` for up to 60 seconds until it sees `event: pipeline.end`. This is a smoke test of the *container*, exercising the job API end-to-end, not just of the source tree the way the `test` job is — a regression in the Dockerfile, the compose wiring, or the job API's happy path fails CI even though every unit test still passes.

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

The schedule assumes ~10 hours/week and aggressive use of Claude Code for scaffolding, tests, and docs. *Status annotated as of 2026-07-02 — the plan's shape held up less well than its spirit: weeks 3-4 went considerably deeper than scoped, and weeks 5-6 delivered different infrastructure (containers + CI) than originally planned (AWS).*

**Weeks 1–2 — Foundation & MVP** *(complete as of 2026-05-31)*
- Project scaffolding, config, structured logging, domain models. ✅
- Ingestion: simulated source, stdin source, GitHub-issues harvester, Loki harvester. ✅
- Detection: fingerprint clustering, PII regex, threshold engine. ✅
- Agent: investigate → reason → confidence gate → generate fix → draft PR → open PR → notify. ✅ *(the `investigate`/`reason` split named here was replaced in weeks 3-4, below.)*
- Hybrid router with mock + real backends. ✅
- Audit storage: in-memory, file (default), OpenSearch. ✅
- Dry-run mode + structured fix-generation with unified-diff display. ✅
- LlamaIndex testbed (sample logs, mock paths, defaults). ✅
- **Live triage UI:** `/app` with top-N issue ranking, SSE event streaming, persisted runs, replay. ✅
- **Verification loop:** sandbox (Docker + subprocess), reproducer synth, baseline/rerun, scoped regression, honest verdict labels. ✅
- ADR-0001 (record decisions), ADR-0002 (hybrid routing), ADR-0003 (LlamaIndex testbed), ADR-0004 (event sinks + SSE), ADR-0005 (verify-fix loop). ✅

**Weeks 3–4 — Intelligence & hybrid routing** *(mostly done, and it went deeper than scoped)*
- ~~Multi-turn agent reasoning (retry fix gen on validation failure; ask for diff with more context).~~ Superseded by a full rewrite: `reason_root_cause`/`expand_context` became an agentic investigator (bounded tool loop, issue-era code reads, forced final synthesis on budget exhaustion), plus a new `ground_root_cause` step with web-search + SDK-schema grounding (§4.3). ✅, went further than scoped.
- Local-model PII classifier (replace regex). ❌ Not started — the regex floor still carries production traffic.
- Smarter code search: combine exception type + nouns from message; fall back to symbol search. Partially subsumed by the investigator's free-seeding and issue-era retrieval (§4.3); no dedicated ranker built.
- Stack-trace extraction from GitHub issue bodies. ✅ (feeds the investigator's free-seed and the SDK-schema probe.)
- Real Anthropic API integration validated against a real LlamaIndex issue (dry-run). ✅
- Slack integration polish. ❌ Not started.
- Repo-ownership guardrail (`TVASTR_ALLOW_UPSTREAM`). ✅
- **Not originally scoped, shipped anyway:** verify-side dependency provisioning, buggy-file overlay, register-aware oracles, bounded reproducer repair loop (§4.7); benchmarking every fix against its linked human PR (§4.3, §4.7); the dashboard run story card and chapter dividers (§4.6). See ADR-0006 and ADR-0007.

**Weeks 5–6 — Containers + CI landed; AWS deployment did not**
- ~~Terraform / CDK for ECS Fargate + Lambda + EventBridge + SQS + OpenSearch.~~ ❌ Not started.
- Dockerise the agent; CI builds & pushes images. ✅ — but as local/CI infrastructure, not an AWS deployment: a multi-stage `Dockerfile`, `docker-compose.yml` (socket-mounted verify sandbox, dual-path sandbox roots, restart policy), and GitHub Actions CI (offline suite + a built-image smoke test through the job API) landed instead (§4.8, ADR-0008).
- Self-hosted Langfuse; instrument every LLM call. ❌ Not started — the `EventSink`/JSONL instrumentation from weeks 1-2 remains the observability story.
- Dashboards: ingestion rate, pattern selection rate, PR-open success rate, dry-run output review queue. ❌ Not started as separate ops dashboards; the run story card (§4.6) covers the per-run version of this.
- Load test: replay 1k synthetic events; confirm the threshold engine and dedup hold. ❌ Not started.

**Weeks 7–10 — Polish, docs, open-source launch**
- README that tells the story (problem, architecture, hybrid routing rationale, demo). ✅ Refreshed 2026-07-02 to reflect everything above (this document + `README.md` + ADR-0006/0007/0008).
- 3-minute screencast: log arrives → pattern fires → agent opens a dry-run PR with a real diff against a LlamaIndex fork. ❌ Not started.
- Blog post: "I built an autonomous code remediation agent — here's what worked and what didn't." ❌ Not started.
- LinkedIn post + Show HN. ❌ Not started.
- Tag v1.0. ❌ Not started.

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
- **Benchmark-reference-PR credibility.** `compare_to_pr` (§4.3, §4.7) treats the issue's linked human PR as the answer key, but that PR is itself a human artifact — sometimes wrong, incomplete, or aimed at a different symptom than the one the agent found. A `divergent` verdict is evidence worth reading, not a ground-truth failing grade, and nothing today flags a reference PR as suspect independently of the comparison itself.
- **Static-vs-runtime schema knowledge boundary.** SDK-schema grounding (§4.3) reads a dependency's installed type definitions as text to ground field names — it can show a field *exists* in a class, not that it's *populated* the way the agent assumes at runtime for the specific response the issue reported. It narrows the search space for a diagnosis; it doesn't substitute for actually executing the code path in question.

## 11. References

- ADR-0001 — Record architecture decisions
- ADR-0002 — Hybrid local/cloud LLM routing
- ADR-0003 — Switch testbed from Haystack to LlamaIndex
- ADR-0004 — Live pipeline instrumentation via event sinks
- ADR-0005 — Verify-fix loop: recreate, patch, prove the failure is gone
- ADR-0006 — Ground diagnosis in issue-era code and installed-SDK reality
- ADR-0007 — Verify oracle hardening (extends ADR-0005)
- ADR-0008 — Job-model run lifecycle: event-sourced runs, containers, CI
- `docs/design-doc.pdf` — original (Haystack-era) design doc, superseded by this file
