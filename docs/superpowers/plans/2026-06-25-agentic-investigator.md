# Agentic Investigator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rigid `investigate → reason_root_cause ⇄ expand_context` diagnosis core with a bounded tool-using investigator sub-agent inside the existing graph shell.

**Architecture:** `_investigate` runs a structured propose→execute loop — the model proposes `search_code`/`read_file`/`list_dir` actions or finishes with a root cause — over the full issue body, guided by a systematic-debugging method prompt. The gate consumes the model's self-reported confidence. Everything downstream (gate, `ground_root_cause`, `generate_fix`, `compare_to_pr`, verify, PII routing, events) is unchanged.

**Tech Stack:** Python 3.12, LangGraph, the project `HybridRouter` (mock/scripted-router test seam), `extract_json`.

## Global Constraints

- The investigator loop is bounded: hard cap `_MAX_INVESTIGATE_ROUNDS = 4`; accumulated files capped at `_MAX_CONTEXT_FILES = 12` (existing constant); dedup reads via a `seen` set.
- Actions: `{"search": q}` → `search_codebase`; `{"read_file": p}` → `retrieve_code_files([p])` merged into `code_files`; `{"list_dir": d}` → `list_dir`. Each emits a `tool.call`. Unknown keys ignored.
- The investigator system prompt MUST encode the four method disciplines: root-cause-first, verify-the-reporter's-hypothesis, cross-reference-sibling-paths, cite-evidence.
- Output: a `RootCause` (`summary`, `suspected_files`, `confidence`, `reasoning`) + accumulated `code_files`/`code_context`. The gate uses `root_cause.confidence` vs `ctx.min_confidence` (0.5, unchanged).
- Never crashes the run: tool/LLM errors are caught and the loop continues/finishes; on non-convergence return a confidence-0.0 "insufficient evidence" RootCause. Missing `issue_body` → fall back to `pattern.title`/`representative_message`.
- PII: every round goes through `router.run` (cloud → redacted); no new bypass.
- Removed: `reason_root_cause`, `expand_context`, `_should_expand`, `_after_reason`, and their tests (`tests/test_agent_reasoning.py`, `tests/test_agent_expand.py`). `_parse_reasoning`, `_search_query_from_message`, `_EVIDENCE_CONFIDENCE`, `_MAX_EXPANSIONS` become dead and are removed.
- Docker/subprocess sandbox, router, verifier, fix-generation: untouched.
- MANDATORY before each commit: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/integrations/github.py` | `list_dir` on the CodeHost protocol + Mock + real client |
| `src/tvastr/agent/tools/code_retrieval.py` | `list_dir(ctx, path)` wrapper |
| `src/tvastr/agent/tools/__init__.py` | export `list_dir` |
| `src/tvastr/agent/state.py` | `issue_body` key |
| `src/tvastr/pipeline.py`, `src/tvastr/api/routes/run.py` | thread `issue_body` to the agent seed |
| `src/tvastr/agent/graph.py` | investigator loop + method prompt; remove old nodes; gate edge |
| `tests/` | `test_sandbox`-style host test; `test_agent_investigate.py` (new); remove `test_agent_reasoning.py`, `test_agent_expand.py` |

---

## Task 1: `list_dir` tool

**Files:** `src/tvastr/integrations/github.py`, `src/tvastr/agent/tools/code_retrieval.py`, `src/tvastr/agent/tools/__init__.py`; Test: `tests/test_list_dir.py` (create)

**Interfaces:**
- Produces: `code_host.list_dir(path: str) -> list[str]` (protocol + Mock + real); `agent.tools.code_retrieval.list_dir(ctx, path) -> list[str]`; exported from `agent.tools`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_list_dir.py`:

```python
from tvastr.agent.context import AgentContext
from tvastr.agent.tools import list_dir
from tvastr.config import Settings
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def test_mock_list_dir_returns_entries():
    host = MockGitHubClient()
    entries = host.list_dir("llama_index/core/memory")
    assert isinstance(entries, list)
    assert all(isinstance(e, str) for e in entries)


def test_list_dir_tool_wraps_host():
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=build_router(settings), code_host=MockGitHubClient(), notifier=build_notifier(settings)
    )
    entries = list_dir(ctx, "llama_index/core")
    assert isinstance(entries, list)


def test_list_dir_tool_degrades_on_error():
    class _Boom:
        def list_dir(self, path):
            raise RuntimeError("nope")

    class _Ctx:
        code_host = _Boom()

    assert list_dir(_Ctx(), "x") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_list_dir.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_dir'` / `MockGitHubClient` has no `list_dir`.

- [ ] **Step 3: Add `list_dir` to the protocol + clients**

In `src/tvastr/integrations/github.py`, add to the `_CodeHostLike` Protocol (after `get_file`):

```python
    def list_dir(self, path: str) -> list[str]: ...
```

Add to `MockGitHubClient` (after its `get_file`):

```python
    def list_dir(self, path: str) -> list[str]:
        log.info("github.list_dir", repo=self.repo, path=path, mocked=True)
        base = path.rstrip("/")
        return [f"{base}/base.py", f"{base}/utils.py"]
```

Add to `GitHubClient` (after its `get_file`):

```python
    def list_dir(self, path: str) -> list[str]:
        try:
            contents = self._repo_handle().get_contents(path, ref=self.base_branch)
        except Exception as exc:
            log.warning("github.list_dir.failed", path=path, error=str(exc))
            return []
        items = contents if isinstance(contents, list) else [contents]
        return [c.path for c in items]
```

> NOTE: use the real client's existing repo accessor. The file caches the repo via a helper (the property used by `get_file` — e.g. `self._repo_handle()` or the cached `self._repo`/property). Match whatever `get_file` calls; if `get_file` uses `repo = self._repo_handle()`, use that here too.

- [ ] **Step 4: Add the tool wrapper + export**

In `src/tvastr/agent/tools/code_retrieval.py`, add:

```python
def list_dir(ctx: AgentContext, path: str) -> list[str]:
    """List files under ``path`` in the target repo; ``[]`` on any failure."""
    try:
        entries = ctx.code_host.list_dir(path)
    except Exception as exc:
        log.warning("tool.list_dir.failed", path=path, error=str(exc))
        return []
    log.info("tool.list_dir", path=path, count=len(entries))
    return entries
```

In `src/tvastr/agent/tools/__init__.py`, add `list_dir` to the imports from `code_retrieval` and to `__all__`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_list_dir.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Regression + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean.

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/integrations/github.py src/tvastr/agent/tools/code_retrieval.py src/tvastr/agent/tools/__init__.py tests/test_list_dir.py
git commit -m "feat(agent): list_dir tool on the code host"
```

---

## Task 2: `issue_body` plumbing

**Files:** `src/tvastr/agent/state.py`, `src/tvastr/pipeline.py`, `src/tvastr/api/routes/run.py`; Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `AgentState["issue_body"]: str | None`; `RemediationPipeline.run(..., issue_body=None)` seeds it into the agent state.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline.py` (reuse its existing `settings`/`recurring_events` fixtures + the `agent.run` spy pattern already present in `test_pipeline_seeds_agent_state_with_pr`):

```python
def test_pipeline_seeds_issue_body(settings, recurring_events, monkeypatch):
    from tvastr.pipeline import build_pipeline

    pipeline = build_pipeline(settings)
    seen = {}
    orig = pipeline.agent.run

    def _spy(state):
        seen.update(state)
        return orig(state)

    monkeypatch.setattr(pipeline.agent, "run", _spy)
    pipeline.run(events=recurring_events, issue_body="full issue text here")
    assert seen.get("issue_body") == "full issue text here"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -k seeds_issue_body -v`
Expected: FAIL — `run()` rejects `issue_body`.

- [ ] **Step 3: Add the state key**

In `src/tvastr/agent/state.py`, add after `evidence_source` (or near the other agent-input keys):

```python
    issue_body: str  # full issue text, for the investigator's starting context
```

- [ ] **Step 4: Thread it through `pipeline.run`**

In `src/tvastr/pipeline.py`, add the param to `run` (alongside `pr_ref`/`pr_diff`):

```python
    def run(
        self,
        events: list[LogEvent] | None = None,
        *,
        run_meta: dict | None = None,
        pr_ref: object | None = None,
        pr_diff: object | None = None,
        issue_body: str | None = None,
    ) -> PipelineRun:
```

And add it to the agent seed (the `self.agent.run({...})` dict — alongside `pr_ref`/`pr_diff`):

```python
            final = self.agent.run(
                {
                    "pattern": pattern,
                    "sample_events": sample_events,
                    "pr_ref": pr_ref,
                    "pr_diff": pr_diff,
                    "issue_body": issue_body,
                }
            )
```

- [ ] **Step 5: Pass it from the run route**

In `src/tvastr/api/routes/run.py`, the route builds an issue record with `body=issue.body or ""`. Pass that body into the existing `pipeline.run(...)` call by adding the kwarg:

```python
            pipeline.run(
                events=events,
                run_meta={...unchanged...},
                pr_ref=pr_ref,
                pr_diff=pr_diff,
                issue_body=issue.body or "",
            )
```

(Use whatever local already holds the issue body — the route reads `issue.body` when building the record; reuse that value.)

- [ ] **Step 6: Run to verify pass + regression + lint**

Run: `uv run pytest tests/test_pipeline.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: new test passes; full suite green; lint clean.

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/agent/state.py src/tvastr/pipeline.py src/tvastr/api/routes/run.py tests/test_pipeline.py
git commit -m "feat(agent): thread the full issue body into the agent state"
```

---

## Task 3: Investigator loop + graph reshape

**Files:** `src/tvastr/agent/graph.py`; Tests: create `tests/test_agent_investigate.py`, remove `tests/test_agent_reasoning.py` + `tests/test_agent_expand.py`

**Interfaces:**
- Consumes: `list_dir` (Task 1), `issue_body` (Task 2), existing `search_codebase`/`retrieve_code_files`/`extract_stack_files`/`format_code_for_prompt`.
- Produces: `_investigate` (the loop) returning `root_cause`/`code_files`/`code_context`/`suspected_files`/`routing`; `_parse_investigation`; `_clamp_confidence`; constant `_MAX_INVESTIGATE_ROUNDS = 4`. Removes `reason_root_cause`/`expand_context`/`_should_expand`/`_after_reason` and the now-dead helpers/constants.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_investigate.py`:

```python
"""Tests for the agentic investigator node."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent, _clamp_confidence, _parse_investigation
from tvastr.config import Settings
from tvastr.domain import FailurePattern, RoutingDecision, Sensitivity
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.llm.base import LLMResponse


def test_clamp_confidence():
    assert _clamp_confidence(0.5) == 0.5
    assert _clamp_confidence(2.0) == 1.0
    assert _clamp_confidence(-1.0) == 0.0
    assert _clamp_confidence("bad") == 0.0


def test_parse_investigation_actions_and_finish():
    a = _parse_investigation('{"thought":"t","actions":[{"search":"q"}]}')
    assert a["actions"] == [{"search": "q"}]
    f = _parse_investigation('{"root_cause":"rc","confidence":0.9,"done":true}')
    assert f["root_cause"] == "rc" and f["done"] is True
    assert _parse_investigation("not json") == {}


class _FakeHost:
    def __init__(self, search_map=None, files=None, dirs=None):
        self.search_map = search_map or {}
        self.files = files or {}
        self.dirs = dirs or {}
        self.reads: list[str] = []

    def search_code(self, query, *, limit=5):
        return list(self.search_map.get(query, []))[:limit]

    def get_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_dir(self, path):
        return list(self.dirs.get(path, []))

    def open_pull_request(self, draft):
        raise NotImplementedError


class _SeqRouter:
    """Returns a scripted sequence of ROOT_CAUSE responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.calls += 1
        text = self._responses.pop(0)
        d = RoutingDecision(task=task.value, target="cloud", model="seq",
                            sensitivity=sensitivity, reason="seq")
        return LLMResponse(text=text, model="seq", target="cloud", mocked=True), d


def _agent(host, router, sink=None):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(router=router, code_host=host, notifier=build_notifier(settings),
                       event_sink=sink or ListEventSink(), run_id="t")
    return RemediationAgent(ctx)


def _pattern():
    return FailurePattern(fingerprint="f", title="WeaviateVectorStore broken filter",
                          representative_message="UnexpectedBehavior: broken filter")


def test_investigate_converges_with_actions_then_finish():
    host = _FakeHost(
        search_map={"weaviate filter": ["weaviate/base.py"]},
        files={"weaviate/base.py": "def query(): by_property('id')", "weaviate/utils.py": "uuid=id"},
        dirs={"weaviate": ["weaviate/base.py", "weaviate/utils.py"]},
    )
    router = _SeqRouter([
        '{"thought":"look","actions":[{"search":"weaviate filter"},{"read_file":"weaviate/base.py"},{"list_dir":"weaviate"},{"read_file":"weaviate/utils.py"}]}',
        '{"root_cause":"query uses by_property(id) but store uses uuid; delete uses by_id","suspected_files":["weaviate/base.py"],"confidence":0.9,"done":true}',
    ])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "no such prop 'id'"})
    assert out["root_cause"].confidence == 0.9
    assert "weaviate/base.py" in out["code_files"]
    assert "weaviate/utils.py" in out["code_files"]
    assert out["root_cause"].suspected_files == ["weaviate/base.py"]


def test_investigate_hard_cap_returns_low_confidence():
    host = _FakeHost()
    # always returns actions, never done -> hit the cap -> insufficient evidence
    router = _SeqRouter(['{"thought":"x","actions":[{"search":"q"}]}'] * 10)
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0
    assert router.calls == 4  # _MAX_INVESTIGATE_ROUNDS


def test_investigate_unparseable_finishes_low_confidence():
    host = _FakeHost()
    router = _SeqRouter(["not json at all"])
    agent = _agent(host, router)
    out = agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    assert out["root_cause"].confidence == 0.0


def test_investigate_prompt_includes_issue_body_and_method():
    captured = {}

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
            captured["prompt"] = prompt
            captured["system"] = system or ""
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter(['{"root_cause":"rc","confidence":0.8,"done":true}'])
    agent = _agent(_FakeHost(), router)
    agent._investigate({"pattern": _pattern(), "sample_events": [], "issue_body": "SENTINEL-BODY"})
    assert "SENTINEL-BODY" in captured["prompt"]
    sysl = captured["system"].lower()
    assert "root cause" in sysl and "cross-reference" in sysl and "hypothesis" in sysl and "evidence" in sysl


def test_investigate_runs_on_act_path_in_graph():
    host = _FakeHost(files={"a.py": "x"})
    router = _SeqRouter(['{"root_cause":"rc","suspected_files":["a.py"],"confidence":0.9,"done":true}'])
    sink = ListEventSink()
    agent = _agent(host, router, sink)
    agent.run({"pattern": _pattern(), "sample_events": [], "issue_body": "b"})
    steps = [e.step for e in sink.events if e.type == "agent.node.start"]
    assert "investigate" in steps
    assert "reason_root_cause" not in steps and "expand_context" not in steps
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_agent_investigate.py -v`
Expected: FAIL — `_parse_investigation`/`_clamp_confidence` undefined; `_investigate` is still the old single-shot.

- [ ] **Step 3: Add helpers + the method prompt + the constant**

In `src/tvastr/agent/graph.py`, add the import `list_dir` to the `tvastr.agent.tools` import block. Add the constant near `_MAX_CONTEXT_FILES`:

```python
_MAX_INVESTIGATE_ROUNDS = 4
```

Add the method system prompt near `_DOC_GROUNDING_SYSTEM`:

```python
_INVESTIGATE_SYSTEM = (
    "You are a senior engineer debugging a reported bug by reading the codebase. "
    "Follow this method strictly:\n"
    "1. ROOT CAUSE FIRST: do not conclude until you have READ the actual code that "
    "proves the cause; if you have not, keep investigating or report low confidence.\n"
    "2. VERIFY THE REPORTER'S HYPOTHESIS: identify the real symptom AND any cause the "
    "reporter guessed, and treat the guess as a hypothesis to confirm against the code, "
    "not as fact.\n"
    "3. CROSS-REFERENCE: compare related/sibling code paths (read vs write vs delete) and "
    "look for the inconsistency that explains the bug.\n"
    "4. CITE EVIDENCE: your root_cause must reference specific file:line; set confidence by "
    "how well-corroborated it is; do not guess.\n\n"
    "Respond with ONLY JSON. To investigate further:\n"
    '{"thought": "...", "actions": [{"search": "terms"}, {"read_file": "path"}, '
    '{"list_dir": "dir"}]}\n'
    "When you have a proven root cause:\n"
    '{"root_cause": "2-4 sentences citing file:line", "suspected_files": ["path"], '
    '"confidence": 0.0-1.0, "done": true}'
)


def _clamp_confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_investigation(text: str) -> dict:
    """The investigator's JSON turn, or {} if unparseable."""
    parsed = extract_json(text)
    return parsed if isinstance(parsed, dict) else {}
```

- [ ] **Step 4: Replace `_investigate` with the loop**

Replace the entire existing `_investigate` method with:

```python
    def _investigate(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        events = state.get("sample_events", [])
        issue_body = state.get("issue_body") or pattern.representative_message or pattern.title
        self._emit("agent.node.start", "investigate", pattern=pattern.fingerprint)

        code_files: dict[str, str] = {}
        seen: set[str] = set()
        transcript: list[str] = []
        decisions: list[RoutingDecision] = []

        # Free seed: stack-trace files when a traceback is present.
        suspected = extract_stack_files(events)
        if suspected:
            self._emit("tool.call", "extract_stack_files", source="stack_trace", paths=suspected)
            fetched = retrieve_code_files(self.ctx, suspected)
            code_files.update(fetched)
            seen.update(suspected)

        root_cause: RootCause | None = None
        for round_i in range(_MAX_INVESTIGATE_ROUNDS):
            prompt = (
                f"Issue: {pattern.title}\n\n{issue_body[:4000]}\n\n"
                f"Code read so far:\n{format_code_for_prompt(code_files) or '(none)'}\n\n"
                f"Tool results so far:\n{chr(10).join(transcript) or '(none)'}\n\n"
                "Investigate further or finish with a proven root_cause."
            )
            try:
                response, decision = self.ctx.router.run(
                    TaskType.ROOT_CAUSE, prompt,
                    sensitivity=pattern.sensitivity, system=_INVESTIGATE_SYSTEM,
                )
            except Exception as exc:
                log.warning("agent.investigate.llm_failed", error=str(exc))
                break
            decisions.append(decision)
            parsed = _parse_investigation(response.text)

            if parsed.get("done") and parsed.get("root_cause"):
                root_cause = RootCause(
                    pattern_id=pattern.id,
                    summary=str(parsed["root_cause"]),
                    suspected_files=[str(p) for p in (parsed.get("suspected_files") or [])]
                    or list(code_files),
                    confidence=_clamp_confidence(parsed.get("confidence", 0.5)),
                    reasoning=response.text,
                )
                break

            actions = parsed.get("actions") or []
            if not actions:
                break  # unparseable / nothing proposed → stop
            self._emit("tool.call", "investigate.round", round=round_i + 1,
                       thought=str(parsed.get("thought", "")))
            for act in actions:
                if not isinstance(act, dict):
                    continue
                if "search" in act:
                    q = str(act["search"])
                    hits = search_codebase(self.ctx, q)
                    self._emit("tool.call", "search_codebase", query=q, paths=hits)
                    transcript.append(f"search {q!r} -> {hits}")
                elif "read_file" in act:
                    p = str(act["read_file"])
                    if p in seen or len(code_files) >= _MAX_CONTEXT_FILES:
                        continue
                    fetched = retrieve_code_files(self.ctx, [p], max_files=1)
                    code_files.update(fetched)
                    seen.add(p)
                    self._emit("tool.call", "retrieve_code_files", requested=1,
                               retrieved=len(fetched), paths=list(fetched.keys()))
                elif "list_dir" in act:
                    d = str(act["list_dir"])
                    entries = list_dir(self.ctx, d)
                    self._emit("tool.call", "list_dir", path=d, entries=entries)
                    transcript.append(f"list_dir {d!r} -> {entries}")

        if root_cause is None:
            root_cause = RootCause(
                pattern_id=pattern.id,
                summary="insufficient evidence to determine the root cause",
                suspected_files=list(code_files),
                confidence=0.0,
                reasoning="investigator did not converge within the round budget",
            )
        self._emit("agent.node.end", "investigate",
                   confidence=root_cause.confidence, summary=root_cause.summary,
                   files_read=len(code_files))
        routing = state.get("routing", [])
        return {
            "root_cause": root_cause,
            "suspected_files": root_cause.suspected_files,
            "code_files": code_files,
            "code_context": format_code_for_prompt(code_files),
            "routing": [*routing, *decisions],
        }
```

- [ ] **Step 5: Remove the old nodes + dead helpers**

Delete these methods entirely from `RemediationAgent`: `_reason_root_cause`, `_expand_context`, `_should_expand`, `_after_reason`. Delete the now-dead module-level helpers/constants: `_parse_reasoning`, `_search_query_from_message`, `_EVIDENCE_CONFIDENCE`, `_MAX_EXPANSIONS`. (Keep `_confidence_gate`, `_MAX_CONTEXT_FILES`, `extract_json`.)

- [ ] **Step 6: Rewire `_build`**

In `_build`, remove the `reason_root_cause` and `expand_context` node registrations and the conditional-edge block + the `expand_context → reason_root_cause` edge. Replace the wiring so investigate flows straight into the gate:

```python
        g.add_node("investigate", self._investigate)
        g.add_node("ground_root_cause", self._ground_root_cause)
        g.add_node("generate_fix", self._generate_fix)
        g.add_node("compare_to_pr", self._compare_to_pr)
        g.add_node("draft_pr", self._draft_pr)
        g.add_node("open_pr", self._open_pr)
        g.add_node("notify", self._notify)

        g.add_edge(START, "investigate")
        g.add_conditional_edges(
            "investigate",
            self._confidence_gate,
            {"act": "ground_root_cause", "skip": "notify"},
        )
        g.add_edge("ground_root_cause", "generate_fix")
        g.add_edge("generate_fix", "compare_to_pr")
        g.add_edge("compare_to_pr", "draft_pr")
        g.add_edge("draft_pr", "open_pr")
        g.add_edge("open_pr", "notify")
        g.add_edge("notify", END)
        return g.compile()
```

(`_confidence_gate` already emits its event and returns `"act"`/`"skip"` — now wired directly off `investigate`.)

- [ ] **Step 7: Update the module docstring flow diagram**

Replace the flow block at the top of the file with:

```
    START -> investigate (agentic loop: search/read/list -> root_cause)
        -> (confidence gate)
        high -> ground_root_cause -> generate_fix -> compare_to_pr -> draft_pr -> open_pr -> notify -> END
        low  -> notify (skipped) -> END
```

- [ ] **Step 8: Remove the superseded test files**

```bash
git rm tests/test_agent_reasoning.py tests/test_agent_expand.py
```

(Their nodes no longer exist; the investigator tests replace them.)

- [ ] **Step 9: Run the investigator tests + full suite + lint**

Run: `uv run pytest tests/test_agent_investigate.py -v && uv run pytest -q && uv run ruff check src tests`
Expected: investigator tests pass; full suite green (the removed-node tests are gone; gate/ground/fix/compare/verify tests still pass); lint clean.

- [ ] **Step 10: Commit**

```bash
git add src/tvastr/agent/graph.py tests/test_agent_investigate.py
git commit -m "feat(agent): agentic investigator replaces reason/expand diagnosis core"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With live mode + tokens, run #15743. Confirm the timeline shows `investigate` rounds (`investigate.round` thoughts + `search_codebase`/`retrieve_code_files`/`list_dir` calls), the agent reads `weaviate/base.py` + `utils.py`, and the `benchmark.compared` verdict moves off `divergent` toward `match`/`partial` (the fix lands on `weaviate/base.py`).

## Self-Review (completed by author)

- **Spec coverage:** bounded loop + cap (Task 3 `_MAX_INVESTIGATE_ROUNDS`, `test_investigate_hard_cap`); actions search/read/list (Task 3 dispatch + `test_investigate_converges`); method prompt's 4 disciplines (Task 3 `_INVESTIGATE_SYSTEM` + `test_investigate_prompt_includes_issue_body_and_method`); self-reported confidence → gate (Task 3 + graph wiring); `list_dir` tool (Task 1); issue_body plumbing (Task 2); node removal + graph reshape + old-test removal (Task 3 Steps 5–8); never-crash (`test_investigate_unparseable`/hard-cap → confidence 0.0); PII via router (loop calls `router.run`); live metric (#15743).
- **Placeholder scan:** none — every step has complete code; the two NOTEs give concrete reuse instructions.
- **Type consistency:** `list_dir(ctx, path) -> list[str]`, `_clamp_confidence(object) -> float`, `_parse_investigation(str) -> dict`, `_MAX_INVESTIGATE_ROUNDS`, `issue_body` state key, `RootCause` output fields — used identically across tasks; gate reads `root_cause.confidence` (unchanged).
