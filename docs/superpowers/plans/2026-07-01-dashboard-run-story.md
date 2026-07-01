# Dashboard Run Story Card + Chapter Dividers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, at-a-glance "run story" card above the pipeline timeline, a plain-English verdict gloss map, and light chapter dividers — so an unfamiliar viewer can tell what the agent did and whether it worked, without losing any raw transparency.

**Architecture:** All changes live in the single self-contained template `src/tvastr/api/templates/app.html` (inline CSS + vanilla JS; the file is served fresh per request, so a browser refresh shows edits). Three additions hook into the existing `appendEvent(event)` funnel, which both live SSE streams and `replayRun` already pass through: a `VERDICT_GLOSS` map, an `updateStory(event, card)` story-card updater, and a `maybeInsertDivider(event)` chapter inserter. Zero backend, Python, or event-schema changes.

**Tech Stack:** Vanilla JS + CSS inside `app.html`. No new dependencies, no JS test harness (the repo has none and this project keeps the dashboard a single file — verification is via the served page and browser, plus the untouched pytest suite staying green).

**Spec:** `docs/superpowers/specs/2026-07-01-dashboard-run-story-design.md`

## Global Constraints

- ONLY `src/tvastr/api/templates/app.html` may be modified. No Python, backend, or event-schema changes. `uv run pytest -q` must stay green (it is untouched by design).
- The story card and glosses are derived 1:1 from events — **no LLM calls, no paraphrase beyond the static gloss map**.
- Every dynamic string rendered into HTML goes through the existing `html()` escaper.
- Degradation rules: missing payload fields render `—` for that fragment; a missing event leaves its row in the pending state; unknown verdicts fall back to a yellow badge with the raw verdict string and no gloss. No code path may throw on old persisted runs.
- The raw event timeline must remain unchanged (cards, expand behavior, payload rendering — except the one gloss line added to the `verify.result` card body).
- `VERDICT_GLOSS` must cover exactly the 13 keys of the existing `VERDICT_BADGE` map.
- Chapter dividers: first-match by chapter order, once each; once a later chapter has started, earlier chapters are sealed (their prefixes are ignored).
- Do NOT use `node --check` to validate the inline script — the installed node is v10 and cannot parse the file's existing optional chaining (`?.`). Syntax verification is via the served page + browser console in the final task.
- CSS uses only existing `:root` variables (`--surface-2`, `--surface-3`, `--border`, `--text`, `--text-dim`, `--error`, `--mono`, `--sans`, etc.).
- Work on branch `feature/dashboard-run-story` off `main`.

## File anchors (orientation for all tasks)

In `src/tvastr/api/templates/app.html` (823 lines at plan time):

- CSS block ends near line 140 (`</style>`); verdict/PR-verdict styles are at lines 111–138.
- `VERDICT_BADGE` map: lines 229–243.
- `runPipeline(req)` clears the timeline at line ~423 (`$("pipeline").innerHTML = "";`).
- `appendEvent(event)`: lines ~551–597; creates `card`, appends via `$("pipeline").appendChild(card);`.
- `appendVerifyAttemptDivider(n)`: lines ~633–638 (inline-styled divider).
- `renderBody(event)`: lines ~713–754, starts with `const p = event.payload || {};` and `const parts = [];`.
- `loadRuns()` builds the Past-runs table; the verdict cell is `const vCell = ...` at line ~780.
- `replayRun(runId)` clears the timeline at line ~802.
- `#pipeline` (class `.pipeline`) is its own scroll container (`max-height:78vh;overflow-y:auto`); its parent `.panel` contains `h2` + `#stage-strip` + `#pipeline`. An element inserted as a sibling **before `#pipeline`** is therefore pinned (never scrolls away).

---

### Task 1: `VERDICT_GLOSS` map + gloss in verify.result card + Past-runs tooltips

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (three edits: after `VERDICT_BADGE` ~line 243; inside `renderBody` ~line 715; the `vCell` line in `loadRuns` ~line 780)

**Interfaces:**
- Produces: `const VERDICT_GLOSS = {...}` — a top-level map from all 13 `VERDICT_BADGE` keys to one plain-English sentence each. Task 2's story card reads it.

- [ ] **Step 1: Add the `VERDICT_GLOSS` map**

Immediately after the closing `};` of `VERDICT_BADGE` (line ~243), insert:

```js
// One honest plain-English sentence per verdict — used in the run story card,
// the verify.result card body, and as tooltips on verdict badges. Static and
// deterministic on purpose: it says only what the events prove.
const VERDICT_GLOSS = {
  verified_via_reproducer:   "Reproduced the failure on the buggy code, applied the agent's fix, re-ran — the failure is gone.",
  verified_via_scoped_tests: "The repository's own tests, scoped to the changed files, pass with the fix applied.",
  verified_via_behavior:     "A behavioral check confirmed the fixed code now returns the expected result.",
  verified_via_warning:      "The reproducer confirmed the intended warning now fires.",
  verified_via_better_error: "The reproducer confirmed the clearer error message now raises.",
  masks_symptom:             "The exception went away but the behavioral check still fails — the symptom is masked, not fixed.",
  unverified_smoke_import_only: "Only an import smoke test ran — no evidence the fix addresses the issue.",
  unverified_doc_only:       "Documentation-only fix — there is no runtime behavior to verify.",
  repro_broken:              "The test script itself broke, so it can't be trusted as evidence either way.",
  no_repro:                  "Couldn't reproduce the bug in the sandbox, so the fix is unproven — an honest 'no evidence', not a failure.",
  still_broken:              "The original failure still occurs with the fix applied.",
  regression:                "The fix resolves the failure but breaks existing repository tests.",
  environmental_error:       "The sandbox itself failed (timeout, network, Docker) — no verdict on the fix.",
};
```

- [ ] **Step 2: Show the gloss in the `verify.result` event card body**

In `renderBody(event)` (~line 713), directly after `const parts = [];`, insert:

```js
  // Verdict gloss: the plain-English meaning of the verdict, above the raw kvs.
  if (event.type === "verify.result" && VERDICT_GLOSS[p.verdict]) {
    parts.push(`<div class="kv"><span class="k">meaning</span><span class="v">${html(VERDICT_GLOSS[p.verdict])}</span></div>`);
  }
```

- [ ] **Step 3: Add gloss tooltips to Past-runs verdict badges**

In `loadRuns()` (~line 780), replace:

```js
        const vCell = v ? `<span class="verdict ${v.kind}">${html(v.label)}</span>` : "—";
```

with:

```js
        const vCell = v ? `<span class="verdict ${v.kind}" title="${html(VERDICT_GLOSS[r.verdict] || "")}">${html(v.label)}</span>` : "—";
```

- [ ] **Step 4: Verify the map is complete and the page still serves**

Run:
```bash
python3 - <<'EOF'
import re, pathlib
src = pathlib.Path("src/tvastr/api/templates/app.html").read_text()
def keys(name):
    block = re.search(name + r"\s*=\s*\{(.*?)\n\};", src, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+):", block, re.M))
badge, gloss = keys("VERDICT_BADGE"), keys("VERDICT_GLOSS")
assert badge == gloss, f"mismatch: badge-only={badge-gloss} gloss-only={gloss-badge}"
print(f"OK: {len(gloss)} verdicts, maps match")
EOF
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8000/app
curl -s http://localhost:8000/app | grep -c "VERDICT_GLOSS"
```
Expected: `OK: 13 verdicts, maps match`, `GET /app -> 200`, and grep count ≥ `3`.

- [ ] **Step 5: Run the Python suite (must be untouched and green)**

Run: `uv run pytest -q`
Expected: all tests pass, no failures.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): VERDICT_GLOSS — plain-English verdict meanings in card body + runs-table tooltips"
```

---

### Task 2: Run story card

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (CSS before `</style>`; JS after `VERDICT_GLOSS`; one-line hooks in `appendEvent`, `runPipeline`, `replayRun`)

**Interfaces:**
- Consumes: `VERDICT_GLOSS` and the existing `VERDICT_BADGE`, `html()`, `$()`.
- Produces: `resetStory()` (rebuilds the card; Task 3 adds one line to it) and `updateStory(event, cardEl)` (called from `appendEvent`). Global `let storyEvidence = {}` and `let verifyAttempts = 0`.

- [ ] **Step 1: Add story-card CSS**

Immediately after the `.pr-verdict.partial,...` rule (~line 138), insert:

```css
  /* Run story card — pinned sibling ABOVE the scrolling #pipeline */
  .story{border-bottom:1px solid var(--border);background:var(--surface-2)}
  .story .story-head{padding:6px 14px;font:600 10px var(--mono);color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em}
  .story .row{display:flex;gap:10px;padding:6px 14px;border-top:1px solid var(--border);font-size:12.5px;line-height:1.45;cursor:pointer}
  .story .row:hover{background:var(--surface-3)}
  .story .row .slot{flex:0 0 96px;font:600 10px var(--mono);color:var(--text-dim);text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
  .story .row .val{flex:1;min-width:0}
  .story .row.pending .val{color:var(--text-dim)}
  .story .row.err .val{color:var(--error)}
  .story .chip{display:inline-block;font:600 10px var(--mono);padding:1px 6px;border-radius:3px;border:1px solid var(--border);color:var(--text-dim);margin-left:6px}
  .story .gloss{display:block;color:var(--text-dim);margin-top:2px}
```

- [ ] **Step 2: Add story-card state + functions**

Immediately after the closing `};` of `VERDICT_GLOSS`, insert:

```js
// ── Run story card ───────────────────────────────────────────────────────────
// Deterministic at-a-glance summary, derived 1:1 from the event stream. Every
// filled row records its source event card; clicking the row jumps to and
// expands that card — every claim is one click from its raw evidence.
const STORY_SLOTS = [
  { key: "issue",     label: "Issue",         pending: "…" },
  { key: "diagnosis", label: "Diagnosis",     pending: "…" },
  { key: "fix",       label: "Fix",           pending: "…" },
  { key: "verify",    label: "Verified?",     pending: "not yet verified" },
  { key: "benchmark", label: "vs. human fix", pending: "…" },
];
let storyEvidence = {};   // slot key → the event card element that is its evidence
let verifyAttempts = 0;

function resetStory() {
  storyEvidence = {};
  verifyAttempts = 0;
  const old = $("story-card");
  if (old) old.remove();
  const card = document.createElement("div");
  card.id = "story-card";
  card.className = "story";
  card.innerHTML =
    `<div class="story-head">Run story — click a row to jump to its evidence</div>` +
    STORY_SLOTS.map(s =>
      `<div class="row pending" data-slot="${s.key}">` +
      `<span class="slot">${s.label}</span>` +
      `<span class="val">${s.pending}</span>` +
      `</div>`).join("");
  card.addEventListener("click", (e) => {
    const row = e.target.closest(".row");
    if (!row) return;
    const target = storyEvidence[row.dataset.slot];
    if (!target) return;
    target.classList.add("expanded");
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("pipeline").parentElement.insertBefore(card, $("pipeline"));
}

function setStoryRow(slot, valHtml, evidence, cls) {
  const story = $("story-card");
  if (!story) return;
  const row = story.querySelector(`.row[data-slot="${slot}"]`);
  if (!row) return;
  row.classList.remove("pending", "err");
  if (cls) row.classList.add(cls);
  row.querySelector(".val").innerHTML = valHtml;
  if (evidence) storyEvidence[slot] = evidence;
}

function updateStory(event, cardEl) {
  if (!$("story-card")) return;
  const p = event.payload || {};
  switch (event.type) {
    case "pipeline.start":
      setStoryRow("issue",
        `${html(p.repo || "—")} #${html(String(p.issue_number ?? "?"))} — ${html(p.issue_title || "")}`,
        cardEl);
      break;
    case "agent.node.end":
      if (event.step === "investigate") {
        const conf = p.confidence != null
          ? `<span class="chip">confidence ${Number(p.confidence).toFixed(2)}</span>` : "";
        setStoryRow("diagnosis", `${html(p.summary || "—")}${conf}`, cardEl);
      }
      break;
    case "agent.node.start":
      // The confidence gate annotates the diagnosis row (act/skip vs threshold).
      if (event.step === "confidence_gate" && p.confidence != null) {
        const row = $("story-card").querySelector('.row[data-slot="diagnosis"]');
        if (row && !row.classList.contains("pending")) {
          const cmp = p.decision === "act" ? "≥" : "<";
          const label = p.decision === "act" ? "acting" : "skipped";
          row.querySelector(".val").innerHTML +=
            `<span class="chip">${Number(p.confidence).toFixed(2)} ${cmp} ${Number(p.threshold).toFixed(2)} → ${label}</span>`;
        }
      }
      break;
    case "fix.generated":
      setStoryRow("fix",
        `${(p.files || []).length} file(s) · ${html(p.register || "—")} — ${html(p.summary || "")}`,
        cardEl);
      break;
    case "verify.result": {
      verifyAttempts++;
      const meta = VERDICT_BADGE[p.verdict] || { kind: "yellow", label: p.verdict || "?" };
      const gloss = VERDICT_GLOSS[p.verdict];
      setStoryRow("verify",
        `${verifyAttempts > 1 ? `<span class="chip">attempt ${verifyAttempts}</span> ` : ""}` +
        `<span class="verdict ${meta.kind}">${html(meta.label)}</span>` +
        (gloss ? `<span class="gloss">${html(gloss)}</span>` : ""),
        cardEl);
      break;
    }
    case "benchmark.compared": {
      const kind = { match: "green", partial: "yellow", divergent: "red" }[p.verdict] || "yellow";
      const both = (p.files_both || []).length;
      const theirs = (p.files_theirs_only || []).length;
      setStoryRow("benchmark",
        `<span class="verdict ${kind}">${html(p.verdict || "?")}</span>` +
        `<span class="gloss">${p.same_root_cause ? "Same root cause as" : "Different root cause from"} ` +
        `the human fix (PR #${html(String(p.pr_number ?? "?"))}) · files overlap ${both}/${both + theirs}</span>`,
        cardEl);
      break;
    }
    case "benchmark.skipped":
      setStoryRow("benchmark",
        `<span class="gloss">not compared — ${html(p.reason || "no upstream PR")}</span>`, cardEl);
      break;
    case "error": {
      // Mark the row for the stage in flight: verify errors hit the verify row;
      // otherwise the earliest still-pending agent-side row.
      const story = $("story-card");
      const slot = event.step === "verify"
        ? "verify"
        : (["diagnosis", "fix", "benchmark"].find(s =>
            story.querySelector(`.row[data-slot="${s}"]`)?.classList.contains("pending")) || "fix");
      setStoryRow(slot, html(`${p.error || "error"}: ${p.message || p.reason || ""}`), cardEl, "err");
      break;
    }
  }
}
```

- [ ] **Step 3: Hook into `appendEvent` and the two reset points**

(a) In `appendEvent(event)`, immediately after `$("pipeline").appendChild(card);` (~line 589), insert:

```js
  updateStory(event, card);
```

(b) In `runPipeline(req)`, immediately after `$("pipeline").innerHTML = "";` (~line 423), insert:

```js
  resetStory();
```

(c) In `replayRun(runId)`, immediately after `$("pipeline").innerHTML = "";` (~line 802), insert:

```js
  resetStory();
```

- [ ] **Step 4: Verify served page contains the card wiring**

Run:
```bash
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8000/app
curl -s http://localhost:8000/app | grep -c "resetStory\|updateStory\|story-card"
```
Expected: `GET /app -> 200`; grep count ≥ `8` (definitions + 3 hooks + CSS/id usages).

- [ ] **Step 5: Run the Python suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): run story card — at-a-glance issue/diagnosis/fix/verify/benchmark with evidence links"
```

---

### Task 3: Chapter dividers + restyled attempt divider

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (CSS before `</style>`; JS after the story-card block; one line in `appendEvent`; one line in `resetStory`; replace `appendVerifyAttemptDivider` body)

**Interfaces:**
- Consumes: `resetStory()` from Task 2 (adds a reset line to it), `html()`, `$()`.
- Produces: `maybeInsertDivider(event)` called from `appendEvent`; global `let seenChapters = new Set()`.

- [ ] **Step 1: Add divider CSS**

Immediately after the `.story .gloss{...}` rule from Task 2, insert:

```css
  /* Chapter dividers in the timeline */
  .chapter{margin:14px 2px 8px;padding:6px 2px 0;font:600 12px var(--sans);color:var(--text);letter-spacing:.01em;border-top:1px solid var(--border)}
  .chapter .n{color:var(--text-dim);font:600 11px var(--mono);margin-right:6px}
  .chapter.sub{font:600 10px var(--mono);color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;border-top:1px dashed var(--border)}
```

- [ ] **Step 2: Add the chapter model + inserter**

Immediately after the closing `}` of `updateStory` (from Task 2), insert:

```js
// ── Chapter dividers ─────────────────────────────────────────────────────────
// The first event of each chapter inserts a plain-English divider. First-match
// by chapter order, once each; once a later chapter has started, earlier
// chapters are sealed (llm./router./tool. events recur during later stages and
// must not re-trigger "Investigating the cause").
const CHAPTERS = [
  { title: "Reading the failure",        prefixes: ["ingest.", "detect.", "threshold."] },
  { title: "Investigating the cause",    prefixes: ["agent.", "tool.", "retrieval.", "doc.", "router.", "llm."] },
  { title: "Writing the fix",            prefixes: ["fix.", "pr."] },
  { title: "Comparing to the human fix", prefixes: ["benchmark."] },
  { title: "Proving it in a sandbox",    prefixes: ["verify."] },
];
let seenChapters = new Set();

function maybeInsertDivider(event) {
  let idx = -1;
  for (let i = 0; i < CHAPTERS.length; i++) {
    if (CHAPTERS[i].prefixes.some(pre => event.type.startsWith(pre))) { idx = i; break; }
  }
  if (idx === -1 || seenChapters.has(idx)) return;
  if ([...seenChapters].some(s => s > idx)) return;  // sealed: a later chapter already started
  seenChapters.add(idx);
  const d = document.createElement("div");
  d.className = "chapter";
  d.innerHTML = `<span class="n">${idx + 1}.</span>${html(CHAPTERS[idx].title)}`;
  $("pipeline").appendChild(d);
}
```

- [ ] **Step 3: Hook into `appendEvent`, reset in `resetStory`, restyle the attempt divider**

(a) In `appendEvent(event)`, immediately before `const card = document.createElement("div");` (~line 568), insert:

```js
  maybeInsertDivider(event);
```

(b) In `resetStory()` (Task 2), immediately after `verifyAttempts = 0;`, insert:

```js
  seenChapters = new Set();
```

(c) Replace the whole body of `appendVerifyAttemptDivider(n)` (~lines 633–638, the version with the long inline `style.cssText`) with:

```js
function appendVerifyAttemptDivider(n) {
  const d = document.createElement("div");
  d.className = "chapter sub";
  d.textContent = `Verification attempt ${n}`;
  $("pipeline").appendChild(d);
}
```

- [ ] **Step 4: Verify served page contains the divider wiring**

Run:
```bash
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8000/app
curl -s http://localhost:8000/app | grep -c "maybeInsertDivider\|seenChapters\|CHAPTERS"
```
Expected: `GET /app -> 200`; grep count ≥ `7`.

- [ ] **Step 5: Run the Python suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): plain-English chapter dividers in the timeline (sealed first-match; restyled attempt divider)"
```

---

### Task 4: End-to-end verification against persisted runs

**Files:**
- No source changes expected (fix-forward only if a defect is found; any fix stays inside `app.html`).

**Interfaces:**
- Consumes: everything from Tasks 1–3, the running app on `http://localhost:8000`, persisted runs in `data/runs/*.jsonl`.

- [ ] **Step 1: Confirm the suite and the served page**

Run:
```bash
uv run pytest -q
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8000/app
```
Expected: suite green; `GET /app -> 200`.

- [ ] **Step 2: Structural replay simulation (no browser)**

Simulate the divider + story logic against a real persisted run's event sequence to catch ordering bugs (this re-implements only the two pure decision rules, in Python, to cross-check the JS):

```bash
python3 - <<'EOF'
import json, pathlib
runs = sorted(pathlib.Path("data/runs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
assert runs, "no persisted runs found"
run = runs[-1]  # newest run
CHAPTERS = [
    ("Reading the failure",        ("ingest.", "detect.", "threshold.")),
    ("Investigating the cause",    ("agent.", "tool.", "retrieval.", "doc.", "router.", "llm.")),
    ("Writing the fix",            ("fix.", "pr.")),
    ("Comparing to the human fix", ("benchmark.",)),
    ("Proving it in a sandbox",    ("verify.",)),
]
seen, order, slots = set(), [], {}
for line in run.read_text().splitlines():
    ev = json.loads(line); t = ev["type"]; p = ev.get("payload") or {}
    idx = next((i for i, (_, pres) in enumerate(CHAPTERS) if any(t.startswith(x) for x in pres)), -1)
    if idx != -1 and idx not in seen and not any(s > idx for s in seen):
        seen.add(idx); order.append(CHAPTERS[idx][0])
    if t == "pipeline.start": slots["issue"] = f"{p.get('repo')} #{p.get('issue_number')}"
    if t == "agent.node.end" and ev.get("step") == "investigate": slots["diagnosis"] = p.get("summary", "")[:60]
    if t == "fix.generated": slots["fix"] = f"{len(p.get('files') or [])} file(s) · {p.get('register', '—')}"
    if t == "verify.result": slots["verify"] = p.get("verdict")
    if t == "benchmark.compared": slots["benchmark"] = f"{p.get('verdict')} · PR #{p.get('pr_number')}"
print(f"run: {run.name}")
print("chapter order:", " → ".join(order))
assert order == [c for c, _ in CHAPTERS if c in order], "chapters out of order!"
for k in ("issue", "diagnosis", "fix", "verify", "benchmark"):
    print(f"  {k:10s}: {slots.get(k, '(pending)')}")
EOF
```
Expected: chapters print in spec order with no assertion error; the filled slots match what you know about that run (a verified #17105 run shows `verify: verified_via_reproducer`, `benchmark: match · PR #21543`).

- [ ] **Step 3: Report for manual browser pass**

The implementer's report must state the structural-simulation output and remind the controller that the following are **human/browser checks** (controller + user perform them; not automatable here):

1. Replay 2–3 persisted runs (incl. #17105's verified run and one older pre-register run): all five rows fill or degrade to `—`; row clicks expand the right event card; each divider appears exactly once, in order.
2. Fresh mock-mode live run: card assembles in real time with pending "…" states.
3. Verify re-run: Verification row updates in place with `attempt 2` chip.
4. Past-runs table: hovering a verdict badge shows the gloss tooltip.

- [ ] **Step 4: Commit (only if a fix was needed)**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "fix(ui): <describe the defect found during e2e verification>"
```
