# Issue-Era Per-File Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the investigator read repository code **as of the issue's creation date**, so moved/renamed/deleted paths resolve and the code still contains the bug — fixing the "investigator reasons correctly but can't find the source on current `main`" failure (#17105).

**Architecture:** Resolve the issue-era commit once from the issue's creation date (`commit_before`), wrap the agent's code host with `IssueEraCodeHost` so `get_file`/`list_dir` serve issue-era content (reusing `get_file_at_ref` + a new `list_dir_at_ref`), and seed the investigator from the issue's own traceback (`extract_issue_files`). `search_code`/PR creation are unchanged. Gated by a flag; default behavior preserved.

**Tech Stack:** Python 3.11/3.12, PyGithub code host, pydantic, LangGraph-style agent, `uv`, `ruff`, `pytest`.

## Global Constraints

- **Zero regression / never crash:** every new path degrades to today's current-`main` reads — no issue date, `commit_before` returns `None`, wrap/fetch fails, or `issue_era_retrieval=false` → unwrapped `ctx.code_host`. All new code-host methods return `None`/`[]` on any error (never raise).
- **Anchor on the issue date** (`sample_events[0].timestamp`), never the fixing PR.
- **Single issue-era ref** = repo HEAD as-of the issue date (one `commit_before` call); all issue-era reads use that one sha.
- **`IssueEraCodeHost` wraps reads only:** `get_file`→`inner.get_file_at_ref(path, sha)` (fallback `inner.get_file`); `list_dir`→`inner.list_dir_at_ref(path, sha)`; `search_code`/`open_pull_request`/`buggy_parent_sha`/`get_file_at_ref`/`commit_before`/`list_dir_at_ref` → delegate to `inner`.
- New code-host methods added to BOTH Protocols (`CodeHost` in `agent/context.py`, `_CodeHostLike` in `integrations/github.py`) and all three concrete clients (`MockGitHubClient`, `GitHubClient`, `DryRunCodeHost`).
- Config flag `issue_era_retrieval: bool = True` (`TVASTR_ISSUE_ERA_RETRIEVAL`); sealed `false` in tests. Flag lives on `AgentContext` (mirroring `doc_grounding`).
- Commit footer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`

---

### Task 1: Code-host methods — `commit_before` + `list_dir_at_ref`

**Files:**
- Modify: `src/tvastr/integrations/github.py` (Protocol + 3 clients)
- Modify: `src/tvastr/agent/context.py` (`CodeHost` Protocol)
- Test: `tests/test_code_host_issue_era.py` (new)

**Interfaces:**
- Produces: `commit_before(self, iso_date: str) -> str | None` (repo HEAD as-of a date); `list_dir_at_ref(self, path: str, ref: str) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_code_host_issue_era.py
from tvastr.integrations.github import DryRunCodeHost, GitHubClient, MockGitHubClient


def test_mock_commit_before_deterministic():
    h = MockGitHubClient("run-llama/llama_index")
    sha = h.commit_before("2024-12-01T00:00:00+00:00")
    assert sha and isinstance(sha, str)
    assert h.commit_before("2024-12-01T00:00:00+00:00") == sha


def test_mock_list_dir_at_ref_returns_entries():
    h = MockGitHubClient("run-llama/llama_index")
    out = h.list_dir_at_ref("a/b", "deadbeef")
    assert out and all(p.startswith("a/b/") for p in out)


def test_dryrun_delegates_issue_era_methods():
    inner = MockGitHubClient("run-llama/llama_index")
    h = DryRunCodeHost(inner, "run-llama/llama_index")
    assert h.commit_before("2024-12-01T00:00:00+00:00") == inner.commit_before(
        "2024-12-01T00:00:00+00:00"
    )
    assert h.list_dir_at_ref("a/b", "ref") == inner.list_dir_at_ref("a/b", "ref")


def test_github_commit_before_uses_until(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    class _Commit:
        sha = "headsha"

    class _Repo:
        def __init__(self):
            self.kwargs = None

        def get_commits(self, **kwargs):
            self.kwargs = kwargs
            return [_Commit()]

    repo = _Repo()
    monkeypatch.setattr(c, "_get_repo", lambda: repo)
    assert c.commit_before("2024-12-01T00:00:00+00:00") == "headsha"
    assert "until" in repo.kwargs


def test_github_commit_before_none_on_error(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(c, "_get_repo", boom)
    assert c.commit_before("2024-12-01T00:00:00+00:00") is None


def test_github_list_dir_at_ref(monkeypatch):
    c = GitHubClient(token="x", repo="r")

    class _Item:
        def __init__(self, p):
            self.path = p

    class _Repo:
        def get_contents(self, path, ref=None):
            assert ref == "myref"
            return [_Item("a/b/x.py"), _Item("a/b/y.py")]

    monkeypatch.setattr(c, "_get_repo", lambda: _Repo())
    assert c.list_dir_at_ref("a/b", "myref") == ["a/b/x.py", "a/b/y.py"]
```

- [ ] **Step 2: Run, watch fail** — `uv run pytest tests/test_code_host_issue_era.py -v` → FAIL (`commit_before` missing).

- [ ] **Step 3: Add to the `CodeHost` Protocol (context.py)**

In `src/tvastr/agent/context.py`, inside `class CodeHost(Protocol)`, after `get_file_at_ref`:

```python
    def commit_before(self, iso_date: str) -> str | None:
        """Return the repo's HEAD commit sha as of a date (ISO 8601), or None."""
        ...

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        """List a directory's entries at a specific commit/ref."""
        ...
```

- [ ] **Step 4: Add to `_CodeHostLike` + the three clients (github.py)**

In `class _CodeHostLike(Protocol)`, after `get_file_at_ref`:

```python
    def commit_before(self, iso_date: str) -> str | None: ...
    def list_dir_at_ref(self, path: str, ref: str) -> list[str]: ...
```

In `MockGitHubClient`, after `get_file_at_ref`:

```python
    def commit_before(self, iso_date: str) -> str | None:
        log.info("github.commit_before", repo=self.repo, date=iso_date, mocked=True)
        return f"era{iso_date[:10].replace('-', '')}"

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        log.info("github.list_dir_at_ref", repo=self.repo, path=path, ref=ref, mocked=True)
        base = path.rstrip("/")
        return [f"{base}/base.py", f"{base}/utils.py"]
```

In `GitHubClient`, after `get_file_at_ref`:

```python
    def commit_before(self, iso_date: str) -> str | None:
        try:
            from datetime import datetime

            until = datetime.fromisoformat(iso_date)
            commits = self._get_repo().get_commits(until=until)
            return str(commits[0].sha)
        except Exception as exc:
            log.warning("github.commit_before.failed", date=iso_date, error=str(exc))
            return None

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        try:
            contents = self._get_repo().get_contents(path, ref=ref)
        except Exception as exc:
            log.warning("github.list_dir_at_ref.failed", path=path, ref=ref, error=str(exc))
            return []
        items = contents if isinstance(contents, list) else [contents]
        return [c.path for c in items]
```

In `DryRunCodeHost`, after `get_file_at_ref`:

```python
    def commit_before(self, iso_date: str) -> str | None:
        return self.inner.commit_before(iso_date)

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        return self.inner.list_dir_at_ref(path, ref)
```

- [ ] **Step 5: Run, pass + lint** — `uv run pytest tests/test_code_host_issue_era.py -v` → all pass; `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/integrations/github.py src/tvastr/agent/context.py tests/test_code_host_issue_era.py
git commit -m "feat(retrieval): code-host commit_before + list_dir_at_ref

<footer>"
```

---

### Task 2: `IssueEraCodeHost` wrapper

**Files:**
- Create: `src/tvastr/integrations/issue_era_host.py`
- Test: `tests/test_issue_era_host.py`

**Interfaces:**
- Consumes: `commit_before`/`get_file_at_ref`/`list_dir_at_ref` (Task 1).
- Produces: `IssueEraCodeHost(inner, sha)` implementing the `CodeHost` protocol; reads served at `sha`, everything else delegates.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_issue_era_host.py
from tvastr.integrations.issue_era_host import IssueEraCodeHost


class _Inner:
    def __init__(self):
        self.calls = []

    def get_file(self, path):
        self.calls.append(("get_file", path)); return f"main:{path}"

    def get_file_at_ref(self, path, ref):
        self.calls.append(("get_file_at_ref", path, ref)); return f"{ref}:{path}"

    def list_dir(self, path):
        self.calls.append(("list_dir", path)); return [f"{path}/main.py"]

    def list_dir_at_ref(self, path, ref):
        self.calls.append(("list_dir_at_ref", path, ref)); return [f"{path}/{ref}.py"]

    def search_code(self, query, *, limit=5):
        self.calls.append(("search_code", query)); return ["s.py"]

    def commit_before(self, iso_date):
        return "innersha"

    def buggy_parent_sha(self, pr_number):
        return "bp"

    def open_pull_request(self, draft):
        self.calls.append(("open_pr",)); return "PR"


def test_reads_served_at_sha():
    inner = _Inner()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.get_file("a/b.py") == "SHA1:a/b.py"
    assert h.list_dir("a") == ["a/SHA1.py"]


def test_get_file_falls_back_to_main_when_ref_miss():
    class _Miss(_Inner):
        def get_file_at_ref(self, path, ref):
            return None  # file absent at ref
    inner = _Miss()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.get_file("a/b.py") == "main:a/b.py"  # fell back to inner.get_file


def test_search_and_pr_delegate_unchanged():
    inner = _Inner()
    h = IssueEraCodeHost(inner, "SHA1")
    assert h.search_code("q") == ["s.py"]
    assert h.open_pull_request(object()) == "PR"
    assert ("search_code", "q") in inner.calls
```

- [ ] **Step 2: Run, watch fail** — `uv run pytest tests/test_issue_era_host.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** `src/tvastr/integrations/issue_era_host.py`:

```python
"""A code host that serves reads as of a fixed commit (the issue-era sha).

Wraps an inner code host: ``get_file``/``list_dir`` resolve at ``sha`` (so the
investigator reads the repository as it was when the bug was reported, even for
paths that moved or were deleted on ``main``); everything else delegates.
"""

from __future__ import annotations

from tvastr.domain import PullRequestDraft, PullRequestResult
from tvastr.logging import get_logger

log = get_logger(__name__)


class IssueEraCodeHost:
    def __init__(self, inner: object, sha: str) -> None:
        self.inner = inner
        self.sha = sha

    def get_file(self, path: str) -> str:
        content = self.inner.get_file_at_ref(path, self.sha)  # type: ignore[attr-defined]
        if content is not None:
            return content
        return self.inner.get_file(path)  # type: ignore[attr-defined]

    def list_dir(self, path: str) -> list[str]:
        return self.inner.list_dir_at_ref(path, self.sha)  # type: ignore[attr-defined]

    # ── everything else delegates to the inner host ──
    def search_code(self, query: str, *, limit: int = 5) -> list[str]:
        return self.inner.search_code(query, limit=limit)  # type: ignore[attr-defined]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        return self.inner.get_file_at_ref(path, ref)  # type: ignore[attr-defined]

    def list_dir_at_ref(self, path: str, ref: str) -> list[str]:
        return self.inner.list_dir_at_ref(path, ref)  # type: ignore[attr-defined]

    def commit_before(self, iso_date: str) -> str | None:
        return self.inner.commit_before(iso_date)  # type: ignore[attr-defined]

    def buggy_parent_sha(self, pr_number: int) -> str | None:
        return self.inner.buggy_parent_sha(pr_number)  # type: ignore[attr-defined]

    def open_pull_request(self, draft: PullRequestDraft) -> PullRequestResult:
        return self.inner.open_pull_request(draft)  # type: ignore[attr-defined]
```

- [ ] **Step 4: Run, pass + lint.**

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/integrations/issue_era_host.py tests/test_issue_era_host.py
git commit -m "feat(retrieval): IssueEraCodeHost serves reads at the issue-era sha

<footer>"
```

---

### Task 3: `extract_issue_files`

**Files:**
- Create: `src/tvastr/agent/retrieval/__init__.py`, `src/tvastr/agent/retrieval/issue_extract.py`
- Test: `tests/test_issue_extract.py`

**Interfaces:**
- Produces: `extract_issue_files(issue_body: str) -> list[str]` — traceback `File "…", line N` paths mapped (best-effort) to repo paths; unmappable dropped; deduped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_issue_extract.py
from tvastr.agent.retrieval.issue_extract import extract_issue_files


def test_extracts_integration_path():
    body = (
        'Traceback:\n  File "/x/site-packages/llama_index/multi_modal_llms/ollama/'
        'base.py", line 28, in get_additional_kwargs\n    ...\n'
    )
    assert extract_issue_files(body) == [
        "llama-index-integrations/multi_modal_llms/"
        "llama-index-multi-modal-llms-ollama/llama_index/multi_modal_llms/ollama/base.py"
    ]


def test_extracts_core_path():
    body = 'File "/x/llama_index/core/program/mm.py", line 5, in run\n'
    assert extract_issue_files(body) == [
        "llama-index-core/llama_index/core/program/mm.py"
    ]


def test_drops_unmappable_and_dedupes():
    body = (
        'File "/app/user_script.py", line 1\n'
        'File "/x/llama_index/core/a.py", line 2\n'
        'File "/y/llama_index/core/a.py", line 9\n'  # dup
    )
    assert extract_issue_files(body) == ["llama-index-core/llama_index/core/a.py"]


def test_empty_body():
    assert extract_issue_files("") == []
```

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement** `src/tvastr/agent/retrieval/issue_extract.py`:

```python
"""Extract suspected source files from an issue's traceback.

Maps an installed/import path (``llama_index/<cat>/<name>/<rest>``) to its
monorepo repo path so the investigator can read it at the issue-era ref. Mapping
is best-effort: unmappable paths are dropped (issue-era ``list_dir`` is the
safety net).
"""

from __future__ import annotations

import re

_FILE_RE = re.compile(r'File "([^"]+)", line \d+')


def _to_repo_path(traceback_path: str) -> str | None:
    p = traceback_path.replace("\\", "/")
    idx = p.rfind("llama_index/")
    if idx == -1:
        return None
    rel = p[idx:]  # llama_index/<...>
    parts = rel.split("/")
    if len(parts) < 3:  # need llama_index/<cat>/<...>
        return None
    if parts[1] == "core":
        return f"llama-index-core/{rel}"
    cat, name = parts[1], parts[2]
    dist = f"llama-index-{cat.replace('_', '-')}-{name}"
    return f"llama-index-integrations/{cat}/{dist}/{rel}"


def extract_issue_files(issue_body: str) -> list[str]:
    if not issue_body:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _FILE_RE.finditer(issue_body):
        repo_path = _to_repo_path(m.group(1))
        if repo_path and repo_path not in seen:
            seen.add(repo_path)
            out.append(repo_path)
    return out
```

Create `src/tvastr/agent/retrieval/__init__.py`:

```python
from tvastr.agent.retrieval.issue_extract import extract_issue_files

__all__ = ["extract_issue_files"]
```

- [ ] **Step 4: Run, pass + lint.**

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/agent/retrieval/__init__.py src/tvastr/agent/retrieval/issue_extract.py tests/test_issue_extract.py
git commit -m "feat(retrieval): extract issue-era suspected files from tracebacks

<footer>"
```

---

### Task 4: Wire it in — flag, pipeline wrap, investigator seed, event

**Files:**
- Modify: `src/tvastr/config.py` (flag)
- Modify: `src/tvastr/agent/context.py` (`AgentContext.issue_era_retrieval`)
- Modify: `src/tvastr/pipeline.py` (set the flag from settings; wrap per pattern; emit event)
- Modify: `src/tvastr/agent/graph.py` (seed `extract_issue_files` in `_investigate`)
- Modify: `tests/conftest.py` (seal the flag)
- Test: `tests/test_issue_era_wiring.py` (new) + add a graph-seed assertion

**Interfaces:**
- Consumes: `IssueEraCodeHost` (Task 2), `commit_before` (Task 1), `extract_issue_files` (Task 3).
- Produces: when `ctx.issue_era_retrieval` and a sha resolves, the agent's `ctx.code_host` is an `IssueEraCodeHost` for that run; `retrieval.issue_era` event `{sha, ok}`; the investigator seeds suspected files from the issue body too.

- [ ] **Step 1: Add the config flag** — in `src/tvastr/config.py`, after `doc_grounding`:

```python
    # When true, the agent reads repository code as of the issue's creation date
    # (so moved/deleted paths resolve and still contain the bug) by wrapping the
    # code host with IssueEraCodeHost. Off in tests.
    issue_era_retrieval: bool = True
```

- [ ] **Step 2: Add the AgentContext field** — in `src/tvastr/agent/context.py`, in the `AgentContext` dataclass, after `doc_grounding: bool = False`:

```python
    issue_era_retrieval: bool = False
```

- [ ] **Step 3: Seal in conftest** — in `tests/conftest.py`, after the `TVASTR_DOC_GROUNDING` line:

```python
os.environ["TVASTR_ISSUE_ERA_RETRIEVAL"] = "false"
```

- [ ] **Step 4: Set the flag from settings (pipeline build)** — in `src/tvastr/pipeline.py`, where `AgentContext(...)` is constructed (alongside `doc_grounding=...`), add:

```python
        issue_era_retrieval=settings.issue_era_retrieval,
```

- [ ] **Step 5: Write the failing wiring test**

```python
# tests/test_issue_era_wiring.py
from datetime import UTC, datetime

from tvastr.agent.context import AgentContext
from tvastr.integrations.github import MockGitHubClient
from tvastr.integrations.issue_era_host import IssueEraCodeHost
from tvastr.pipeline import RemediationPipeline  # adjust if the wrap helper lives elsewhere


def test_pipeline_wraps_code_host_at_issue_era(make_pipeline_and_event):
    # See implementer note: build a pipeline whose agent.ctx has
    # issue_era_retrieval=True and a MockGitHubClient code host, run it on one
    # synthetic issue event (created_at set), and assert the agent saw an
    # IssueEraCodeHost during the run + a retrieval.issue_era event fired.
    ...
```

> Implementer note: read `tests/test_pipeline.py` for how it constructs a
> `RemediationPipeline` (or `build_pipeline`) and feeds events, and how it
> captures emitted events (the sink). Build the assertion around: (a) `ctx`
> created with `issue_era_retrieval=True`, code host = `MockGitHubClient`; (b)
> after `run`, a `retrieval.issue_era` event with a non-null `sha` was emitted;
> (c) the wrap is the `IssueEraCodeHost` type (capture it via a spy on the agent,
> or assert `isinstance(self.agent.ctx.code_host, IssueEraCodeHost)` if the wrap
> is left in place after the run). Match the existing test file's helpers; the
> binding contract is "wrap happens + event fired when flag on + sha resolves",
> and "no wrap when flag off". Add the flag-off case too.

- [ ] **Step 6: Implement the pipeline wrap** — in `src/tvastr/pipeline.py` `run()`, capture the base host before the pattern loop and wrap per pattern:

```python
        base_code_host = self.agent.ctx.code_host
        outcomes: list[PatternOutcome] = []
        for pattern in selected:
            sample_events = [
                events_by_id[eid] for eid in pattern.sample_event_ids if eid in events_by_id
            ]
            # Issue-era retrieval: serve the agent's reads as of the issue date.
            self.agent.ctx.code_host = self._issue_era_host(base_code_host, sample_events)
            self._emit("agent.start", "agent", layer="agent",
                       pattern=pattern.fingerprint, title=pattern.title)
            final = self.agent.run({ ... })   # unchanged
```

Add the helper method on `RemediationPipeline`:

```python
    def _issue_era_host(self, base_host, sample_events):
        if not getattr(self.agent.ctx, "issue_era_retrieval", False) or not sample_events:
            return base_host
        try:
            iso = sample_events[0].timestamp.isoformat()
            sha = base_host.commit_before(iso)
        except Exception as exc:
            log.warning("retrieval.issue_era.resolve_failed", error=str(exc))
            sha = None
        self._emit("retrieval.issue_era", "agent", layer="agent",
                   sha=sha or "", ok=bool(sha))
        if not sha:
            return base_host
        from tvastr.integrations.issue_era_host import IssueEraCodeHost

        return IssueEraCodeHost(base_host, sha)
```

(Ensure `log = get_logger(__name__)` exists in pipeline.py; it does.)

- [ ] **Step 7: Seed the investigator from the issue body** — in `src/tvastr/agent/graph.py` `_investigate`, where the stack-trace seed is built (`suspected = extract_stack_files(events)`), merge issue-body files:

Add the import at the top of `graph.py` (with the other tool imports):

```python
from tvastr.agent.retrieval import extract_issue_files
```

Replace the seed block:

```python
        # Free seed: stack-trace files + files named in the issue's own traceback.
        suspected = extract_stack_files(events)
        for f in extract_issue_files(state.get("issue_body") or ""):
            if f not in suspected:
                suspected.append(f)
        if suspected:
            self._emit("tool.call", "extract_stack_files", source="stack_trace", paths=suspected)
            fetched = retrieve_code_files(self.ctx, suspected)
            code_files.update(fetched)
            seen.update(suspected)
```

- [ ] **Step 8: Run the wiring tests + full suite + lint**

Run: `uv run pytest tests/test_issue_era_wiring.py tests/test_pipeline.py -v` → pass.
Run: `uv run pytest -q` → all green (report count).
Run: `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/config.py src/tvastr/agent/context.py src/tvastr/pipeline.py src/tvastr/agent/graph.py tests/conftest.py tests/test_issue_era_wiring.py
git commit -m "feat(retrieval): wire issue-era code host + traceback seeding into the run

<footer>"
```

---

### Task 5: Dashboard — render `retrieval.issue_era`

**Files:**
- Modify: `src/tvastr/api/templates/app.html`

**Interfaces:**
- Consumes: the `retrieval.issue_era` event `{sha, ok}`.

- [ ] **Step 1: Add a summary label** — in `src/tvastr/api/templates/app.html`, in the `summarize` switch (near the other event cases, e.g. after `verify.start` / alongside the agent-layer events), add:

```javascript
    case "retrieval.issue_era": return `issue-era reads @ ${(p.sha||"").slice(0,8)}${p.ok?"":" · unavailable (current main)"}`;
```

(If the dashboard maintains an explicit event-type allowlist that gates which events render, add `"retrieval.issue_era"` to it as well; if events render generically, the summary case is sufficient — check the file.)

- [ ] **Step 2: Verify the page parses + the case is present**

Run: `uv run python -c "from pathlib import Path; import tvastr.api.app as a; html=(Path(a.__file__).resolve().parent/'templates'/'app.html').read_text(); assert 'retrieval.issue_era' in html; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Suite + lint** — `uv run pytest -q` → green; `uv run ruff check src tests` → `All checks passed!`.

- [ ] **Step 4: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(retrieval): surface retrieval.issue_era in the dashboard timeline

<footer>"
```

---

## Self-Review

**Spec coverage:**
- `commit_before` + `list_dir_at_ref` on both Protocols + 3 clients → Task 1. ✓
- `IssueEraCodeHost` (reads at sha, fallback, delegate rest) → Task 2. ✓
- `extract_issue_files` (traceback → repo path, core + integration + unmappable) → Task 3. ✓
- Flag on AgentContext + config + conftest seal → Task 4 Steps 1-3. ✓
- Pipeline resolves sha from issue date + wraps + emits event → Task 4 Steps 4,6. ✓
- Investigator seeds from issue body → Task 4 Step 7. ✓
- Never-crash / degrade (no date/sha/flag-off → base host) → Task 4 `_issue_era_host`. ✓
- UI → Task 5. ✓
- search_code/PR unchanged (delegate) → Task 2. ✓

**Placeholder scan:** Task 4 Step 5's wiring test is described as a contract with an implementer note to match `tests/test_pipeline.py` helpers (the pipeline construction varies); all production code blocks are complete. `<footer>` = the two-line co-author/session footer.

**Type consistency:** `commit_before(iso_date: str) -> str | None`, `list_dir_at_ref(path, ref) -> list[str]`, `IssueEraCodeHost(inner, sha)`, `extract_issue_files(body) -> list[str]`, `issue_era_retrieval` (config + AgentContext + settings) — consistent across all tasks. The wrap reads via `get_file_at_ref`/`list_dir_at_ref` defined in Task 1.
