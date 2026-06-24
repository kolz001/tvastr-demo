# PR-aware triage + agent-vs-PR benchmark — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover each issue's pull request, let the LLM analyze it, and grade tvastr's generated fix against the maintainer's PR as ground truth (match/partial/divergent), streamed into the run timeline.

**Architecture:** A new `src/tvastr/analysis/` package holds PR discovery (GitHub Search API, no LLM), per-card PR analysis (one cloud LLM call), and fix-vs-PR comparison (one cloud LLM call). Two read endpoints feed the triage UI; the comparison runs as a new agent-graph node and emits events into the existing SSE run stream.

**Tech Stack:** Python 3.12, FastAPI, httpx (with `MockTransport` in tests), Pydantic v2 dataclasses, LangGraph, pytest. Single-file vanilla-JS UI (`app.html`).

## Global Constraints

- Python 3.12+; `from __future__ import annotations` at the top of every new module.
- Ruff line-length = 100; run `uv run ruff check` and `uv run ruff format` before each commit.
- All new network calls accept a `transport: object | None = None` param so tests inject `httpx.MockTransport` (mirror `src/tvastr/ingestion/comments.py`).
- Mock mode (`settings.use_mocks` true OR no `GITHUB_TOKEN`/`anthropic_api_key`): discovery returns `None`; analysis/comparison return deterministic stubs. Everything runs offline.
- Cloud LLM calls go through `HybridRouter.run(...)` and produce a `RoutingDecision` recorded in the audit trail — never call a client directly.
- Diffs are capped before reaching an LLM: **≤30 files, ≤1500 total patch lines**; set a `truncated` flag when clipped.
- Caches mirror `comments.py`: 10-min TTL, bounded size, `threading.Lock`-guarded.
- Run tests with `uv run pytest`. Commit after each task with a green suite.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/tvastr/analysis/__init__.py` | Package exports |
| `src/tvastr/analysis/pr_discovery.py` | `PullRequestRef`, `discover_pr`, `PrFile`, `PrDiff`, `fetch_pr_diff` (Search API + diff fetch + caps + cache) |
| `src/tvastr/analysis/pr_analysis.py` | `PrAnalysis`, `analyze_pr` (one cloud LLM call) |
| `src/tvastr/analysis/fix_comparison.py` | `FixComparison`, `compare_fix_to_pr` (one cloud LLM call + deterministic file-set arithmetic) |
| `src/tvastr/llm/router.py` (modify) | Add `PR_ANALYSIS`, `FIX_COMPARISON` task types |
| `src/tvastr/api/routes/pr.py` | `GET /api/issue-pr`, `POST /api/pr-analysis` |
| `src/tvastr/api/app.py` (modify) | Register the new router |
| `src/tvastr/events.py` (modify) | Add `benchmark.compared`, `benchmark.skipped` event types |
| `src/tvastr/agent/state.py` (modify) | Add `pr_ref`, `pr_diff`, `fix_comparison` keys |
| `src/tvastr/agent/graph.py` (modify) | New `compare_to_pr` node between `generate_fix` and `draft_pr` |
| `src/tvastr/pipeline.py` (modify) | Thread `pr_ref`/`pr_diff` into agent state |
| `src/tvastr/api/routes/run.py` (modify) | Discover PR for the chosen issue; pass into the pipeline |
| `src/tvastr/api/templates/app.html` (modify) | PR chip, top-5 auto-analysis spinner, manual button, verdict card |
| `tests/test_pr_discovery.py` | discovery + diff caps |
| `tests/test_pr_analysis.py` | analysis verdicts + mock mode |
| `tests/test_fix_comparison.py` | comparison verdicts + file-set arithmetic |
| `tests/test_pr_api.py` | endpoints (mock mode + stubbed transport) |
| `tests/test_agent_compare.py` | `compare_to_pr` node behavior |

**Shared test helper (used across analysis tests).** Define inline in each test file that needs it:

```python
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse


class StubRouter:
    """Returns a canned LLM response; records the last prompt for assertions."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.last_prompt = prompt
        self.last_system = system
        resp = LLMResponse(text=self._text, model="stub", mocked=True)
        decision = RoutingDecision(
            task=task.value, target="cloud", model="stub",
            sensitivity=sensitivity, reason="stub",
        )
        return resp, decision
```

---

# PHASE 1 — PR discovery + per-card analysis + UI

### Task 1: PR discovery (`discover_pr`)

**Files:**
- Create: `src/tvastr/analysis/__init__.py`
- Create: `src/tvastr/analysis/pr_discovery.py`
- Test: `tests/test_pr_discovery.py`

**Interfaces:**
- Produces: `PullRequestRef(number:int, title:str, state:str, merged:bool, url:str, changed_files:int)`; `discover_pr(repo:str, number:int, *, token:str|None, use_mocks:bool=False, transport=None) -> PullRequestRef | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_discovery.py
from __future__ import annotations

import httpx

from tvastr.analysis.pr_discovery import PullRequestRef, discover_pr


def _search_response(items):
    return httpx.Response(200, json={"total_count": len(items), "items": items})


def _pr_item(number, title, state, merged_at=None):
    return {
        "number": number,
        "title": title,
        "state": state,
        "pull_request": {"merged_at": merged_at},
        "html_url": f"https://github.com/o/r/pull/{number}",
    }


def test_discover_prefers_open_then_merged_then_closed():
    items = [
        _pr_item(10, "closed unmerged", "closed"),
        _pr_item(20, "merged", "closed", merged_at="2026-01-01T00:00:00Z"),
        _pr_item(30, "open fix", "open"),
    ]
    transport = httpx.MockTransport(lambda req: _search_response(items))
    ref = discover_pr("o/r", 19293, token="t", transport=transport)
    assert ref is not None
    assert ref.number == 30 and ref.state == "open"


def test_discover_returns_none_when_no_prs():
    transport = httpx.MockTransport(lambda req: _search_response([]))
    assert discover_pr("o/r", 1, token="t", transport=transport) is None


def test_discover_returns_none_in_mock_mode():
    assert discover_pr("o/r", 1, token=None, use_mocks=True) is None
    assert discover_pr("o/r", 1, token=None) is None


def test_discover_marks_merged():
    items = [_pr_item(20, "merged", "closed", merged_at="2026-01-01T00:00:00Z")]
    transport = httpx.MockTransport(lambda req: _search_response(items))
    ref = discover_pr("o/r", 1, token="t", transport=transport)
    assert ref.merged is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: tvastr.analysis.pr_discovery`.

- [ ] **Step 3: Create the package + implement discovery**

```python
# src/tvastr/analysis/__init__.py
"""PR-aware analysis: discovery, LLM analysis, and agent-fix comparison."""
```

```python
# src/tvastr/analysis/pr_discovery.py
"""Discover the pull request that addresses an issue, and fetch its diff.

Timeline cross-reference linking is sparse in practice (maintainers rarely
write "closes #N"), so discovery uses the GitHub Search API
(``repo:X type:pr <issue#>``) and ranks candidates: open > merged > closed,
tie-broken by most recently updated. No LLM is involved here — this is the
cheap step that feeds the analysis and comparison layers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from tvastr.ingestion.github_api import API_ROOT, github_headers
from tvastr.logging import get_logger

log = get_logger(__name__)

_MAX_DIFF_FILES = 30
_MAX_DIFF_LINES = 1500


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    title: str
    state: str  # "open" | "closed"
    merged: bool
    url: str
    changed_files: int = 0


def _rank(item: dict) -> tuple[int, str]:
    """Sort key: lower rank first. open=0, merged=1, closed=2; then -updated."""
    state = item.get("state", "closed")
    merged = bool((item.get("pull_request") or {}).get("merged_at"))
    rank = 0 if state == "open" else (1 if merged else 2)
    return (rank, "-" + str(item.get("updated_at", "")))


# Cache mirrors comments.py: 10-min TTL, bounded, lock-guarded.
_CACHE_TTL_S = 600.0
_CACHE_MAX = 1024
_cache: dict[tuple[str, int], tuple[float, PullRequestRef | None]] = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return True, hit[1]
        if hit:
            del _cache[key]
    return False, None


def _cache_put(key, value):
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (time.monotonic(), value)


def discover_pr(
    repo: str,
    number: int,
    *,
    token: str | None,
    use_mocks: bool = False,
    transport: object | None = None,
) -> PullRequestRef | None:
    """Find the most relevant PR addressing issue ``number``. None if none/offline."""
    if use_mocks or not token:
        return None

    key = (repo, number)
    found, cached = _cache_get(key)
    if found:
        return cached

    import httpx

    q = f"repo:{repo} type:pr {number}"
    url = f"{API_ROOT}/search/issues"
    try:
        with httpx.Client(timeout=15.0, transport=transport) as client:  # type: ignore[arg-type]
            resp = client.get(url, headers=github_headers(token), params={"q": q, "per_page": "20"})
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as exc:
        log.warning("analysis.discover_pr.failed", repo=repo, number=number, error=str(exc))
        return None

    if not items:
        _cache_put(key, None)
        return None

    best = sorted(items, key=_rank)[0]
    ref = PullRequestRef(
        number=int(best["number"]),
        title=str(best.get("title", "")),
        state=str(best.get("state", "closed")),
        merged=bool((best.get("pull_request") or {}).get("merged_at")),
        url=str(best.get("html_url", "")),
    )
    _cache_put(key, ref)
    log.info("analysis.discover_pr", repo=repo, number=number, pr=ref.number, state=ref.state)
    return ref
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pr_discovery.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/analysis tests/test_pr_discovery.py && uv run ruff format src/tvastr/analysis tests/test_pr_discovery.py
git add src/tvastr/analysis tests/test_pr_discovery.py
git commit -m "feat(analysis): discover the PR addressing an issue via Search API"
```

---

### Task 2: PR diff fetch with caps (`fetch_pr_diff`)

**Files:**
- Modify: `src/tvastr/analysis/pr_discovery.py`
- Test: `tests/test_pr_discovery.py`

**Interfaces:**
- Produces: `PrFile(filename:str, status:str, additions:int, deletions:int, patch:str)`; `PrDiff(files:list[PrFile], truncated:bool)`; `fetch_pr_diff(repo:str, pr_number:int, *, token:str|None, transport=None) -> PrDiff`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_pr_discovery.py
from tvastr.analysis.pr_discovery import PrDiff, fetch_pr_diff


def _file(name, patch, status="modified", add=1, dele=0):
    return {"filename": name, "status": status, "additions": add,
            "deletions": dele, "patch": patch}


def test_fetch_pr_diff_returns_files():
    files = [_file("a.py", "@@ -1 +1 @@\n-old\n+new")]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert isinstance(diff, PrDiff)
    assert diff.files[0].filename == "a.py"
    assert diff.truncated is False


def test_fetch_pr_diff_caps_file_count():
    files = [_file(f"f{i}.py", "@@\n+x") for i in range(40)]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert len(diff.files) == 30
    assert diff.truncated is True


def test_fetch_pr_diff_caps_total_lines():
    big = "\n".join("+line" for _ in range(2000))
    files = [_file("big.py", big)]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=files))
    diff = fetch_pr_diff("o/r", 30, token="t", transport=transport)
    assert diff.truncated is True
    assert sum(f.patch.count("\n") + 1 for f in diff.files) <= 1500 + 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_discovery.py -k diff -v`
Expected: FAIL — `ImportError: cannot import name 'PrDiff'`.

- [ ] **Step 3: Implement diff fetch + caps**

```python
# add to src/tvastr/analysis/pr_discovery.py

@dataclass(frozen=True)
class PrFile:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str


@dataclass(frozen=True)
class PrDiff:
    files: list[PrFile]
    truncated: bool = False


def fetch_pr_diff(
    repo: str,
    pr_number: int,
    *,
    token: str | None,
    transport: object | None = None,
) -> PrDiff:
    """Fetch a PR's changed files, capped at 30 files / 1500 patch lines."""
    import httpx

    url = f"{API_ROOT}/repos/{repo}/pulls/{pr_number}/files"
    with httpx.Client(timeout=20.0, transport=transport) as client:  # type: ignore[arg-type]
        resp = client.get(url, headers=github_headers(token), params={"per_page": "100"})
        resp.raise_for_status()
        raw = resp.json()

    files: list[PrFile] = []
    truncated = len(raw) > _MAX_DIFF_FILES
    total_lines = 0
    for item in raw[:_MAX_DIFF_FILES]:
        patch = str(item.get("patch") or "")
        lines = patch.count("\n") + 1 if patch else 0
        if total_lines + lines > _MAX_DIFF_LINES:
            remaining = max(0, _MAX_DIFF_LINES - total_lines)
            patch = "\n".join(patch.splitlines()[:remaining]) + "\n… (diff truncated)"
            truncated = True
        total_lines += lines
        files.append(
            PrFile(
                filename=str(item.get("filename", "")),
                status=str(item.get("status", "")),
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
                patch=patch,
            )
        )
        if total_lines >= _MAX_DIFF_LINES:
            truncated = truncated or len(raw) > len(files)
            break
    return PrDiff(files=files, truncated=truncated)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pr_discovery.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/analysis && uv run ruff format src/tvastr/analysis tests/test_pr_discovery.py
git add src/tvastr/analysis/pr_discovery.py tests/test_pr_discovery.py
git commit -m "feat(analysis): fetch capped PR diffs"
```

---

### Task 3: Router task type `PR_ANALYSIS`

**Files:**
- Modify: `src/tvastr/llm/router.py:35-41`
- Test: `tests/test_router.py`

**Interfaces:**
- Produces: `TaskType.PR_ANALYSIS` routed to `"cloud"`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_router.py
from tvastr.domain import Sensitivity
from tvastr.llm.router import TaskType, build_router


def test_pr_analysis_routes_to_cloud(settings):
    router = build_router(settings)
    assert router.route_target(TaskType.PR_ANALYSIS, Sensitivity.INTERNAL) == "cloud"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router.py -k pr_analysis -v`
Expected: FAIL — `AttributeError: PR_ANALYSIS`.

- [ ] **Step 3: Add the enum member**

In `src/tvastr/llm/router.py`, add to `TaskType` (after `PR_DESCRIPTION = "pr_description"`):

```python
    PR_ANALYSIS = "pr_analysis"
```

(No change to `_LOCAL_TASKS` — non-local tasks default to cloud.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_router.py -k pr_analysis -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/llm/router.py tests/test_router.py
git commit -m "feat(router): add PR_ANALYSIS cloud task type"
```

---

### Task 4: PR analysis (`analyze_pr`)

**Files:**
- Create: `src/tvastr/analysis/pr_analysis.py`
- Test: `tests/test_pr_analysis.py`

**Interfaces:**
- Consumes: `PullRequestRef`, `PrDiff` (Task 1-2); `HybridRouter.run` (returns `(LLMResponse, RoutingDecision)`); `TaskType.PR_ANALYSIS`.
- Produces: `PrAnalysis(addresses_issue:str, approach_summary:str, key_files:list[str], root_cause:str, pr_number:int, pr_state:str)`; `analyze_pr(issue_title, issue_body, pr_ref, pr_diff, router) -> tuple[PrAnalysis, RoutingDecision]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_analysis.py
from __future__ import annotations

import json

from tvastr.analysis.pr_analysis import PrAnalysis, analyze_pr
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse


class StubRouter:
    def __init__(self, text):
        self._text = text
        self.last_prompt = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.last_prompt = prompt
        decision = RoutingDecision(
            task=task.value, target="cloud", model="stub",
            sensitivity=sensitivity, reason="stub")
        return LLMResponse(text=self._text, model="stub", mocked=True), decision


def _ref():
    return PullRequestRef(30, "fix: token counting", "open", False,
                          "https://github.com/o/r/pull/30", 2)


def _diff():
    return PrDiff(files=[PrFile("llama_index/core/llms.py", "modified", 5, 1, "@@\n+fix")])


def test_analyze_pr_parses_structured_verdict():
    payload = json.dumps({
        "addresses_issue": "yes",
        "approach_summary": "Adds usage extraction for Gemini.",
        "key_files": ["llama_index/core/llms.py"],
        "root_cause": "Token usage not parsed from the response.",
    })
    router = StubRouter(payload)
    analysis, decision = analyze_pr("title", "body", _ref(), _diff(), router)
    assert isinstance(analysis, PrAnalysis)
    assert analysis.addresses_issue == "yes"
    assert analysis.key_files == ["llama_index/core/llms.py"]
    assert analysis.pr_number == 30
    assert decision.task == "pr_analysis"
    assert "fix" in router.last_prompt  # diff reached the prompt


def test_analyze_pr_tolerates_unparseable_response():
    analysis, _ = analyze_pr("t", "b", _ref(), _diff(), StubRouter("not json"))
    assert analysis.addresses_issue == "unknown"
    assert analysis.pr_number == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_analysis.py -v`
Expected: FAIL — `ModuleNotFoundError: tvastr.analysis.pr_analysis`.

- [ ] **Step 3: Implement analysis**

```python
# src/tvastr/analysis/pr_analysis.py
"""LLM analysis of a discovered PR: what it changes, whether it addresses the
issue, and the approach. One cloud call; tolerant of unparseable responses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You are a senior engineer triaging open-source bugs. Given an issue and a "
    "candidate pull request's diff, assess whether the PR addresses the issue. "
    "Respond ONLY with JSON matching the requested schema."
)
_SCHEMA_HINT = (
    'Return JSON: {"addresses_issue": "yes"|"partial"|"no", '
    '"approach_summary": "<=3 sentences", "key_files": ["path", ...], '
    '"root_cause": "1-2 sentences"}'
)
_VALID = {"yes", "partial", "no"}


@dataclass(frozen=True)
class PrAnalysis:
    addresses_issue: str  # yes | partial | no | unknown
    approach_summary: str
    key_files: list[str]
    root_cause: str
    pr_number: int
    pr_state: str
    raw: str = ""


def _diff_blob(diff: PrDiff) -> str:
    parts = [f"--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions})\n{f.patch}"
             for f in diff.files]
    blob = "\n\n".join(parts) or "(no diff available)"
    if diff.truncated:
        blob += "\n\n(NOTE: diff truncated for length.)"
    return blob


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def analyze_pr(
    issue_title: str,
    issue_body: str | None,
    pr_ref: PullRequestRef,
    pr_diff: PrDiff,
    router,
) -> tuple[PrAnalysis, RoutingDecision]:
    prompt = (
        f"ISSUE: {issue_title}\n\n{(issue_body or '')[:2000]}\n\n"
        f"CANDIDATE PR #{pr_ref.number} ({pr_ref.state}): {pr_ref.title}\n\n"
        f"DIFF:\n{_diff_blob(pr_diff)}\n\n{_SCHEMA_HINT}"
    )
    response, decision = router.run(
        TaskType.PR_ANALYSIS, prompt, sensitivity=Sensitivity.INTERNAL, system=_SYSTEM
    )
    parsed = _extract_json(response.text)
    if parsed is None:
        log.warning("analysis.analyze_pr.unparseable", pr=pr_ref.number)
        analysis = PrAnalysis(
            addresses_issue="unknown",
            approach_summary=response.text.strip()[:500],
            key_files=[],
            root_cause="",
            pr_number=pr_ref.number,
            pr_state=pr_ref.state,
            raw=response.text,
        )
        return analysis, decision

    verdict = str(parsed.get("addresses_issue", "")).lower()
    analysis = PrAnalysis(
        addresses_issue=verdict if verdict in _VALID else "unknown",
        approach_summary=str(parsed.get("approach_summary", "")),
        key_files=[str(p) for p in parsed.get("key_files", []) if p],
        root_cause=str(parsed.get("root_cause", "")),
        pr_number=pr_ref.number,
        pr_state=pr_ref.state,
        raw=response.text,
    )
    return analysis, decision
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pr_analysis.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/analysis && uv run ruff format src/tvastr/analysis tests/test_pr_analysis.py
git add src/tvastr/analysis/pr_analysis.py tests/test_pr_analysis.py
git commit -m "feat(analysis): LLM analysis of a discovered PR"
```

---

### Task 5: API endpoints (`/api/issue-pr`, `/api/pr-analysis`)

**Files:**
- Create: `src/tvastr/api/routes/pr.py`
- Modify: `src/tvastr/api/app.py` (register router)
- Test: `tests/test_pr_api.py`

**Interfaces:**
- Consumes: `discover_pr`, `fetch_pr_diff`, `analyze_pr`, `build_router`, `get_settings`.
- Produces: `GET /api/issue-pr?repo&number -> {pr: {...}|null}`; `POST /api/pr-analysis {repo, number} -> PrAnalysisOut | {error}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_api.py
from __future__ import annotations

from fastapi.testclient import TestClient

from tvastr.api import create_app

client = TestClient(create_app())


def test_issue_pr_mock_mode_returns_null():
    # Default settings use_mocks=True → discovery returns null, no crash.
    resp = client.get("/api/issue-pr", params={"repo": "o/r", "number": 1})
    assert resp.status_code == 200
    assert resp.json()["pr"] is None


def test_pr_analysis_mock_mode_returns_unavailable():
    resp = client.post("/api/pr-analysis", json={"repo": "o/r", "number": 1})
    assert resp.status_code == 200
    body = resp.json()
    # No PR discoverable offline → analysis unavailable, surfaced honestly.
    assert body["pr"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_api.py -v`
Expected: FAIL — 404 (routes not registered).

- [ ] **Step 3: Implement the routes**

```python
# src/tvastr/api/routes/pr.py
"""PR discovery + analysis endpoints for the triage UI.

GET  /api/issue-pr     cheap: find the PR addressing an issue (no LLM)
POST /api/pr-analysis  one cloud LLM call analyzing that PR
Both return null/unavailable gracefully in mock mode or when no PR exists.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from tvastr.analysis.pr_analysis import analyze_pr
from tvastr.analysis.pr_discovery import discover_pr, fetch_pr_diff
from tvastr.config import get_settings
from tvastr.ingestion.comments import fetch_issue_body
from tvastr.llm.router import build_router

router = APIRouter(tags=["pr"])


class PrRefOut(BaseModel):
    number: int
    title: str
    state: str
    merged: bool
    url: str


class IssuePrOut(BaseModel):
    pr: PrRefOut | None


class PrAnalysisRequest(BaseModel):
    repo: str
    number: int


class PrAnalysisOut(BaseModel):
    pr: PrRefOut | None
    addresses_issue: str | None = None
    approach_summary: str | None = None
    key_files: list[str] = []
    root_cause: str | None = None


@router.get("/api/issue-pr", response_model=IssuePrOut)
def issue_pr(repo: str, number: int) -> IssuePrOut:
    s = get_settings()
    ref = discover_pr(repo, number, token=s.github_token, use_mocks=s.use_mocks)
    if ref is None:
        return IssuePrOut(pr=None)
    return IssuePrOut(pr=PrRefOut(**{
        "number": ref.number, "title": ref.title, "state": ref.state,
        "merged": ref.merged, "url": ref.url}))


@router.post("/api/pr-analysis", response_model=PrAnalysisOut)
def pr_analysis(req: PrAnalysisRequest) -> PrAnalysisOut:
    s = get_settings()
    ref = discover_pr(req.repo, req.number, token=s.github_token, use_mocks=s.use_mocks)
    if ref is None:
        return PrAnalysisOut(pr=None)

    diff = fetch_pr_diff(req.repo, ref.number, token=s.github_token)
    body = fetch_issue_body(req.repo, req.number, token=s.github_token)
    router_ = build_router(s)
    analysis, _ = analyze_pr(f"#{req.number}", body, ref, diff, router_)
    return PrAnalysisOut(
        pr=PrRefOut(number=ref.number, title=ref.title, state=ref.state,
                    merged=ref.merged, url=ref.url),
        addresses_issue=analysis.addresses_issue,
        approach_summary=analysis.approach_summary,
        key_files=analysis.key_files,
        root_cause=analysis.root_cause,
    )
```

In `src/tvastr/api/app.py`, follow the existing pattern. Add `pr` to the top-level route import (line 11):

```python
from tvastr.api.routes import health, issues, pr, remediate, run, verify
```

And register it alongside the others (after `app.include_router(issues.router)`):

```python
    app.include_router(pr.router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pr_api.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/api && uv run ruff format src/tvastr/api/routes/pr.py tests/test_pr_api.py
git add src/tvastr/api/routes/pr.py src/tvastr/api/app.py tests/test_pr_api.py
git commit -m "feat(api): /api/issue-pr and /api/pr-analysis endpoints"
```

---

### Task 6: Frontend — PR chip, top-5 auto-analysis, manual button

**Files:**
- Modify: `src/tvastr/api/templates/app.html`
- Test: manual (the UI has no JS test harness; verify via the running server).

**Interfaces:**
- Consumes: `GET /api/issue-pr`, `POST /api/pr-analysis`.

- [ ] **Step 1: Add a per-card PR slot + state.** In the issue-card render (where each issue card's HTML is built), add a container after the existing badges:

```html
<div class="pr-slot" data-number="${issue.number}"></div>
```

- [ ] **Step 2: Add discovery + analysis JS.** After the issues-rendered hook, add:

```javascript
async function loadPrForCard(repo, number, slot, autoAnalyze) {
  const r = await fetch(`/api/issue-pr?repo=${encodeURIComponent(repo)}&number=${number}`);
  const { pr } = await r.json();
  if (!pr) return;
  slot.innerHTML =
    `<a class="pr-chip" href="${pr.url}" target="_blank">PR #${pr.number} · ${pr.state} ↗</a>` +
    `<button class="pr-analyze">Analyze PR</button>` +
    `<div class="pr-analysis"></div>`;
  const btn = slot.querySelector(".pr-analyze");
  const out = slot.querySelector(".pr-analysis");
  btn.onclick = () => analyzePr(repo, number, btn, out);
  if (autoAnalyze) analyzePr(repo, number, btn, out);
}

async function analyzePr(repo, number, btn, out) {
  btn.disabled = true;
  out.innerHTML = `<span class="spinner"></span> analyzing PR…`;
  try {
    const r = await fetch("/api/pr-analysis", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, number }),
    });
    const a = await r.json();
    if (!a.pr || !a.addresses_issue) { out.textContent = "analysis unavailable"; return; }
    out.innerHTML =
      `<div class="pr-verdict ${a.addresses_issue}">addresses issue: ${a.addresses_issue}</div>` +
      `<div>${a.approach_summary || ""}</div>` +
      `<div class="muted">files: ${(a.key_files || []).join(", ")}</div>`;
  } catch (e) {
    out.textContent = "analysis failed";
  } finally {
    btn.disabled = false;
  }
}
```

- [ ] **Step 3: Wire discovery on load (top-5 auto).** Where issues finish rendering, iterate cards and call discovery; auto-analyze only the first 5 that have a PR:

```javascript
let autoBudget = 5;
document.querySelectorAll(".pr-slot").forEach((slot) => {
  const number = Number(slot.dataset.number);
  // discovery is cheap; auto-analyze decremented only when a PR is actually found
  fetch(`/api/issue-pr?repo=${encodeURIComponent(currentRepo)}&number=${number}`)
    .then((r) => r.json())
    .then(({ pr }) => {
      if (!pr) return;
      const auto = autoBudget > 0;
      if (auto) autoBudget--;
      loadPrForCardWithRef(currentRepo, number, slot, pr, auto);
    });
});
```

To avoid a second discovery call, refactor `loadPrForCard` into `loadPrForCardWithRef(repo, number, slot, pr, autoAnalyze)` that takes the already-fetched `pr` and renders the chip/button (the body of `loadPrForCard` from Step 2, minus the initial fetch). Keep `currentRepo` in sync with the repo input used by the issues fetch.

- [ ] **Step 4: Add CSS.** Add a spinner + chip styles near the existing styles:

```css
.spinner { display:inline-block; width:12px; height:12px; border:2px solid #888;
  border-top-color:transparent; border-radius:50%; animation:spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.pr-chip { font-size:12px; padding:2px 6px; border-radius:4px; background:#23304a; }
.pr-verdict.yes { color:#4ade80; } .pr-verdict.no { color:#f87171; }
.pr-verdict.partial, .pr-verdict.unknown { color:#fbbf24; }
.pr-analyze { font-size:12px; margin-left:6px; }
```

- [ ] **Step 5: Manual verification.**

```bash
pkill -f "uvicorn tvastr"; uv run uvicorn tvastr.api.app:create_app --factory --host 127.0.0.1 --port 8000 &
sleep 2
```

Open http://127.0.0.1:8000/app. Confirm: PR chips appear on cards that have PRs; the first 5 such cards auto-show a spinner then an analysis; later cards show an "Analyze PR" button that works on click. (In live mode with a token. In mock mode no chips appear — expected.)

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): per-card PR chip, top-5 auto-analysis, manual analyze button"
```

---

# PHASE 2 — agent-fix-vs-PR benchmark

### Task 7: Fix comparison (`compare_fix_to_pr`) + `FIX_COMPARISON` task

**Files:**
- Modify: `src/tvastr/llm/router.py` (add `FIX_COMPARISON = "fix_comparison"` to `TaskType`)
- Create: `src/tvastr/analysis/fix_comparison.py`
- Test: `tests/test_fix_comparison.py`

**Interfaces:**
- Consumes: `FixProposal`/`FileChange` (`tvastr.domain`), `PullRequestRef`, `PrDiff`, `HybridRouter.run`, `TaskType.FIX_COMPARISON`.
- Produces: `FixComparison(verdict:str, same_root_cause:bool, files_both:list[str], files_ours_only:list[str], files_theirs_only:list[str], equivalence:str, rationale:str, confidence:float)`; `compare_fix_to_pr(issue_title, root_cause_summary, our_fix, pr_ref, pr_diff, router) -> tuple[FixComparison, RoutingDecision]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fix_comparison.py
from __future__ import annotations

import json

from tvastr.analysis.fix_comparison import FixComparison, compare_fix_to_pr
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.domain import FileChange, FixProposal, RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse


class StubRouter:
    def __init__(self, text):
        self._text = text
        self.last_prompt = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.last_prompt = prompt
        d = RoutingDecision(task=task.value, target="cloud", model="stub",
                            sensitivity=sensitivity, reason="stub")
        return LLMResponse(text=self._text, model="stub", mocked=True), d


def _fix(paths):
    return FixProposal(pattern_id="p", summary="our fix",
                       changes=[FileChange(path=p, patched_content="x") for p in paths])


def _pr_diff(paths):
    return PrDiff(files=[PrFile(p, "modified", 1, 0, "@@\n+x") for p in paths])


def _ref():
    return PullRequestRef(30, "fix", "open", False, "u", 1)


def test_file_overlap_is_computed_in_code():
    router = StubRouter(json.dumps({
        "verdict": "match", "same_root_cause": True,
        "equivalence": "functionally_equivalent",
        "rationale": "same change", "confidence": 0.9}))
    cmp, _ = compare_fix_to_pr(
        "t", "rc", _fix(["a.py", "b.py"]), _ref(), _pr_diff(["a.py", "c.py"]), router)
    assert isinstance(cmp, FixComparison)
    assert cmp.files_both == ["a.py"]
    assert cmp.files_ours_only == ["b.py"]
    assert cmp.files_theirs_only == ["c.py"]
    assert cmp.verdict == "match"
    assert cmp.same_root_cause is True


def test_unparseable_response_degrades_to_divergent_low_confidence():
    cmp, _ = compare_fix_to_pr(
        "t", "rc", _fix(["a.py"]), _ref(), _pr_diff(["b.py"]), StubRouter("garbage"))
    assert cmp.verdict == "divergent"
    assert cmp.confidence == 0.0
    assert cmp.files_both == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fix_comparison.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Add the task type, then implement comparison**

Add to `TaskType` in `src/tvastr/llm/router.py`:

```python
    FIX_COMPARISON = "fix_comparison"
```

```python
# src/tvastr/analysis/fix_comparison.py
"""Grade tvastr's generated fix against the maintainer's PR (ground truth).

File overlap is computed deterministically in code; the LLM judges root-cause
agreement, functional equivalence, the overall verdict, and a rationale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
from tvastr.domain import FixProposal, RoutingDecision, Sensitivity
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You compare an autonomous agent's proposed fix against a human maintainer's "
    "pull request for the same bug. Judge whether they target the same root cause "
    "and are functionally equivalent. Respond ONLY with JSON."
)
_SCHEMA_HINT = (
    'Return JSON: {"verdict": "match"|"partial"|"divergent", '
    '"same_root_cause": true|false, '
    '"equivalence": "functionally_equivalent"|"same_goal_different_approach"|'
    '"addresses_different_cause", "rationale": "2-4 sentences", '
    '"confidence": 0.0-1.0}'
)
_VERDICTS = {"match", "partial", "divergent"}
_EQUIV = {"functionally_equivalent", "same_goal_different_approach",
          "addresses_different_cause"}


@dataclass(frozen=True)
class FixComparison:
    verdict: str
    same_root_cause: bool
    files_both: list[str]
    files_ours_only: list[str]
    files_theirs_only: list[str]
    equivalence: str
    rationale: str
    confidence: float


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def compare_fix_to_pr(
    issue_title: str,
    root_cause_summary: str,
    our_fix: FixProposal,
    pr_ref: PullRequestRef,
    pr_diff: PrDiff,
    router,
) -> tuple[FixComparison, RoutingDecision]:
    ours = {c.path for c in our_fix.changes}
    theirs = {f.filename for f in pr_diff.files}
    files_both = sorted(ours & theirs)
    files_ours_only = sorted(ours - theirs)
    files_theirs_only = sorted(theirs - ours)

    our_blob = "\n\n".join(
        f"--- {c.path}\n{c.diff or c.patched_content[:800]}" for c in our_fix.changes
    ) or "(no changes)"
    their_blob = "\n\n".join(f"--- {f.filename}\n{f.patch}" for f in pr_diff.files) or "(none)"
    prompt = (
        f"ISSUE: {issue_title}\nAGENT ROOT CAUSE: {root_cause_summary}\n\n"
        f"AGENT FIX:\n{our_blob}\n\n"
        f"HUMAN PR #{pr_ref.number}:\n{their_blob}\n\n"
        f"File overlap (computed): both={files_both}, agent_only={files_ours_only}, "
        f"human_only={files_theirs_only}\n\n{_SCHEMA_HINT}"
    )
    response, decision = router.run(
        TaskType.FIX_COMPARISON, prompt, sensitivity=Sensitivity.INTERNAL, system=_SYSTEM
    )
    parsed = _extract_json(response.text)
    if parsed is None:
        log.warning("analysis.compare.unparseable", pr=pr_ref.number)
        return (
            FixComparison(
                verdict="divergent", same_root_cause=False, files_both=files_both,
                files_ours_only=files_ours_only, files_theirs_only=files_theirs_only,
                equivalence="addresses_different_cause",
                rationale="Comparison response could not be parsed.", confidence=0.0,
            ),
            decision,
        )

    verdict = str(parsed.get("verdict", "")).lower()
    equiv = str(parsed.get("equivalence", "")).lower()
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        FixComparison(
            verdict=verdict if verdict in _VERDICTS else "divergent",
            same_root_cause=bool(parsed.get("same_root_cause", False)),
            files_both=files_both,
            files_ours_only=files_ours_only,
            files_theirs_only=files_theirs_only,
            equivalence=equiv if equiv in _EQUIV else "addresses_different_cause",
            rationale=str(parsed.get("rationale", "")),
            confidence=max(0.0, min(1.0, confidence)),
        ),
        decision,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fix_comparison.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/analysis src/tvastr/llm/router.py && uv run ruff format src/tvastr/analysis tests/test_fix_comparison.py
git add src/tvastr/llm/router.py src/tvastr/analysis/fix_comparison.py tests/test_fix_comparison.py
git commit -m "feat(analysis): compare agent fix against maintainer PR"
```

---

### Task 8: Events + agent state for the benchmark

**Files:**
- Modify: `src/tvastr/events.py:26-55` (EventType Literal)
- Modify: `src/tvastr/agent/state.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces: event types `"benchmark.compared"`, `"benchmark.skipped"`; `AgentState` keys `pr_ref` (`PullRequestRef | None`), `pr_diff` (`PrDiff | None`), `fix_comparison` (`FixComparison | None`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_events.py
from tvastr.events import EventType  # noqa: F401


def test_benchmark_event_types_exist():
    import typing
    args = typing.get_args(EventType)
    assert "benchmark.compared" in args
    assert "benchmark.skipped" in args
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_events.py -k benchmark -v`
Expected: FAIL — assertion error (types absent).

- [ ] **Step 3: Add event types + state keys**

In `src/tvastr/events.py`, add to the `EventType` Literal (after `"verify.result",`):

```python
    "benchmark.compared",
    "benchmark.skipped",
```

In `src/tvastr/agent/state.py`, add an import and three keys. Top imports:

```python
from tvastr.analysis.fix_comparison import FixComparison
from tvastr.analysis.pr_discovery import PrDiff, PullRequestRef
```

Inside `AgentState`:

```python
    pr_ref: PullRequestRef | None
    pr_diff: PrDiff | None
    fix_comparison: FixComparison | None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_events.py -k benchmark -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/events.py src/tvastr/agent/state.py tests/test_events.py
git commit -m "feat: benchmark event types + agent state keys"
```

---

### Task 9: Agent graph `compare_to_pr` node

**Files:**
- Modify: `src/tvastr/agent/graph.py` (`_build` edges + new node method)
- Test: `tests/test_agent_compare.py`

**Interfaces:**
- Consumes: `state["pr_ref"]`, `state["pr_diff"]`, `state["fix"]`, `state["root_cause"]`, `state["pattern"]`; `compare_fix_to_pr`.
- Produces: `state["fix_comparison"]`; emits `benchmark.compared` or `benchmark.skipped`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_compare.py
from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def _agent(sink):
    # Mirrors tests/test_agent_investigate.py::_ctx, plus an event_sink so we
    # can assert on emitted benchmark events.
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=build_router(settings),
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
        event_sink=sink,
        run_id="t",
    )
    return RemediationAgent(ctx)
```

The two assertions that matter:

```python
def test_compare_emits_skipped_without_pr_ref():
    sink = ListEventSink()
    agent = _agent(sink)
    pattern = FailurePattern(fingerprint="f", title="ModuleNotFoundError in x",
                             representative_message="ModuleNotFoundError: no mod")
    agent.run({"pattern": pattern, "sample_events": []})  # no pr_ref in state
    assert any(e.type == "benchmark.skipped" for e in sink.events)
    assert not any(e.type == "benchmark.compared" for e in sink.events)


def test_compare_emits_compared_with_pr_ref():
    sink = ListEventSink()
    agent = _agent(sink)
    pattern = FailurePattern(fingerprint="f", title="ModuleNotFoundError in x",
                             representative_message="ModuleNotFoundError: no mod")
    ref = PullRequestRef(30, "fix", "open", False, "u", 1)
    diff = PrDiff(files=[PrFile("x.py", "modified", 1, 0, "@@\n+x")])
    agent.run({"pattern": pattern, "sample_events": [], "pr_ref": ref, "pr_diff": diff})
    assert any(e.type == "benchmark.compared" for e in sink.events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_compare.py -v`
Expected: FAIL — no `benchmark.*` events emitted (node doesn't exist).

- [ ] **Step 3: Implement the node + rewire edges**

In `src/tvastr/agent/graph.py`, import at top:

```python
from tvastr.analysis.fix_comparison import compare_fix_to_pr
```

In `_build`, register the node and change the edges so `generate_fix → compare_to_pr → draft_pr`:

```python
        g.add_node("compare_to_pr", self._compare_to_pr)
        ...
        g.add_edge("generate_fix", "compare_to_pr")
        g.add_edge("compare_to_pr", "draft_pr")
```

(Remove the old `g.add_edge("generate_fix", "draft_pr")`.)

Add the node method (after `_generate_fix`):

```python
    def _compare_to_pr(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        pr_ref = state.get("pr_ref")
        pr_diff = state.get("pr_diff")
        if pr_ref is None or pr_diff is None:
            self._emit("benchmark.skipped", "compare_to_pr", reason="no upstream PR found")
            return {}
        self._emit("agent.node.start", "compare_to_pr", pr=pr_ref.number)
        try:
            comparison, decision = compare_fix_to_pr(
                pattern.title,
                state["root_cause"].summary,
                state["fix"],
                pr_ref,
                pr_diff,
                self.ctx.router,
            )
        except Exception as exc:  # never crash the run
            log.warning("agent.compare_to_pr.failed", error=str(exc))
            self._emit("benchmark.skipped", "compare_to_pr", reason=f"comparison error: {exc}")
            return {}
        self._emit(
            "benchmark.compared",
            "compare_to_pr",
            verdict=comparison.verdict,
            same_root_cause=comparison.same_root_cause,
            equivalence=comparison.equivalence,
            files_both=comparison.files_both,
            files_ours_only=comparison.files_ours_only,
            files_theirs_only=comparison.files_theirs_only,
            rationale=comparison.rationale,
            confidence=comparison.confidence,
            pr_number=pr_ref.number,
            pr_url=pr_ref.url,
        )
        return {"fix_comparison": comparison, "routing": _append_routing(state, decision)}
```

> NOTE: the confidence gate routes low-confidence patterns to `notify` (skipping `generate_fix`), so `compare_to_pr` only runs on the act path — which is correct (no fix means nothing to compare). When the gate skips, no `benchmark.*` event is emitted; that's acceptable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_compare.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/agent && uv run ruff format src/tvastr/agent tests/test_agent_compare.py
git add src/tvastr/agent/graph.py tests/test_agent_compare.py
git commit -m "feat(agent): compare_to_pr node grades fix vs upstream PR"
```

---

### Task 10: Thread the PR through pipeline + run route

**Files:**
- Modify: `src/tvastr/pipeline.py:79-135` (accept `pr_ref`/`pr_diff`, seed agent state)
- Modify: `src/tvastr/api/routes/run.py` (discover PR, fetch diff, pass to pipeline)
- Test: `tests/test_pipeline.py`, `tests/test_triage_api.py`

**Interfaces:**
- Consumes: `discover_pr`, `fetch_pr_diff`.
- Produces: `RemediationPipeline.run(..., pr_ref=None, pr_diff=None)` seeds each pattern's agent state with `pr_ref`/`pr_diff`.

- [ ] **Step 1: Write the failing test (pipeline passes pr_ref into agent state)**

```python
# append to tests/test_pipeline.py
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef


def test_pipeline_seeds_agent_state_with_pr(settings, recurring_events, monkeypatch):
    from tvastr.pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    seen = {}
    orig = pipeline.agent.run

    def _spy(state):
        seen.update(state)
        return orig(state)

    monkeypatch.setattr(pipeline.agent, "run", _spy)
    ref = PullRequestRef(30, "fix", "open", False, "u", 1)
    diff = PrDiff(files=[PrFile("x.py", "modified", 1, 0, "@@\n+x")])
    pipeline.run(events=recurring_events, pr_ref=ref, pr_diff=diff)
    assert seen.get("pr_ref") == ref
    assert seen.get("pr_diff") == diff
```

> NOTE: reuse the `settings` and `recurring_events` fixtures from `tests/conftest.py`. `recurring_events` already produces a pattern above the threshold, so `agent.run` is called at least once.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -k seeds_agent_state -v`
Expected: FAIL — `run()` rejects `pr_ref`/`pr_diff` kwargs.

- [ ] **Step 3: Implement plumbing**

In `src/tvastr/pipeline.py`, change the `run` signature and the agent invocation:

```python
    def run(
        self,
        events: list[LogEvent] | None = None,
        *,
        run_meta: dict | None = None,
        pr_ref: object | None = None,
        pr_diff: object | None = None,
    ) -> PipelineRun:
```

And where it calls the agent (currently `self.agent.run({"pattern": pattern, "sample_events": sample_events})`):

```python
            final = self.agent.run({
                "pattern": pattern,
                "sample_events": sample_events,
                "pr_ref": pr_ref,
                "pr_diff": pr_diff,
            })
```

In `src/tvastr/api/routes/run.py`, inside `_run` (before building the pipeline), discover the PR for this issue and pass it through. After `events = issue_to_events(...)` and the empty-events guard, add:

```python
            from tvastr.analysis.pr_discovery import discover_pr, fetch_pr_diff

            pr_ref = discover_pr(repo, issue.number, token=settings.github_token,
                                 use_mocks=settings.use_mocks)
            pr_diff = None
            if pr_ref is not None:
                try:
                    pr_diff = fetch_pr_diff(repo, pr_ref.number, token=settings.github_token)
                except Exception as exc:
                    log.warning("run.pr_diff_failed", error=str(exc))
                    pr_ref = None
```

Then pass them into the existing `pipeline.run(...)` call:

```python
            pipeline.run(
                events=events,
                run_meta={...unchanged...},
                pr_ref=pr_ref,
                pr_diff=pr_diff,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pipeline.py tests/test_triage_api.py -v`
Expected: PASS (existing triage tests still green; new pipeline test passes).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/tvastr/pipeline.py src/tvastr/api/routes/run.py && uv run ruff format src/tvastr/pipeline.py src/tvastr/api/routes/run.py tests/test_pipeline.py
git add src/tvastr/pipeline.py src/tvastr/api/routes/run.py tests/test_pipeline.py
git commit -m "feat: thread discovered PR into the agent run for benchmarking"
```

---

### Task 11: Frontend — benchmark verdict card

**Files:**
- Modify: `src/tvastr/api/templates/app.html`
- Test: manual.

**Interfaces:**
- Consumes: `benchmark.compared` / `benchmark.skipped` events from the `/api/run` SSE stream.

- [ ] **Step 1: Add a label/summary case** for the new event types where other event types are mapped (near the `pipeline.start`/`fix.generated` cases):

```javascript
case "benchmark.compared":
  return `verdict=${p.verdict} · same root cause=${p.same_root_cause} · PR #${p.pr_number}`;
case "benchmark.skipped":
  return p.reason || "no upstream PR";
```

- [ ] **Step 2: Add a verdict badge** in the card head (reuse the verify verdict-badge pattern). Where `verify.result` adds `extraHead`, add an analogous branch:

```javascript
if (event.type === "benchmark.compared") {
  const kind = { match: "green", partial: "yellow", divergent: "red" }[event.payload?.verdict] || "yellow";
  extraHead = ` <span class="verdict ${kind}">${html(event.payload?.verdict || "?")}</span>`;
}
```

- [ ] **Step 3: Add a stage chip** (optional) — add `"compare_to_pr"` to the stage list at the top of the pipeline rail so the stage lights up. Find the `STAGES`/stage-map array and add `compare_to_pr` between `generate_fix` and `draft_pr`.

- [ ] **Step 4: Manual verification.**

```bash
pkill -f "uvicorn tvastr"; uv run uvicorn tvastr.api.app:create_app --factory --host 127.0.0.1 --port 8000 &
sleep 2
curl -fsS -N -X POST http://127.0.0.1:8000/api/run -H 'Content-Type: application/json' \
  -d '{"repo":"run-llama/llama_index","issue_number":21062}' | grep -E 'benchmark'
```

Expected (live mode, issue with a discoverable PR and a stack trace that clears the confidence gate): a `benchmark.compared` event with a `verdict`. For an issue with no PR: `benchmark.skipped`. In the UI, the timeline shows the verdict card with a colored badge.

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): render agent-vs-PR benchmark verdict in the run timeline"
```

---

## Final verification (after all tasks)

- [ ] Run the full suite + lint:

```bash
uv run pytest -q && uv run ruff check src tests
```

Expected: all green (existing 127 + the new tests), lint clean.

- [ ] Smoke the live flow end-to-end (server running, token present): load `/app`, confirm PR chips + top-5 auto-analysis, click "Apply fix" on an issue with a PR, watch the `benchmark.compared` verdict appear in the timeline.

---

## Self-review notes (author)

- **Spec coverage:** discovery (Task 1), diff caps (Task 2), PR_ANALYSIS task (3), analysis (4), endpoints (5), UI chips/auto-5/manual (6); FIX_COMPARISON + comparison (7), events+state (8), node (9), pipeline/route plumbing (10), verdict UI (11). All spec sections map to a task.
- **Mock mode:** discovery returns `None` offline → endpoints return null/unavailable → UI shows no chips; agent emits `benchmark.skipped`. Covered in Tasks 1, 5, 9.
- **Error handling:** discovery/diff/analysis/comparison all degrade (return None / unavailable / divergent-0.0 / `benchmark.skipped`) and never raise into the run. Covered in Tasks 1, 4, 7, 9, 10.
- **Type consistency:** `PullRequestRef`, `PrDiff`, `PrFile`, `PrAnalysis`, `FixComparison` field names are used identically across analysis modules, state, node, and tests.
