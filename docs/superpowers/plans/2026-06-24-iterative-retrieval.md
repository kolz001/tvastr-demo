# Iterative (hypothesis-driven) Retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the remediation agent loop reason→retrieve→reason so it fetches the *right* code (chasing the hypothesis its reasoning produces) before generating a fix.

**Architecture:** A bounded LangGraph conditional-edge loop between `reason_root_cause` and a new `expand_context` node. `reason_root_cause` now also emits retrieval directives (`need_more_context` + `next_targets`); a router (`_after_reason`) sends the run to `expand_context` (which fetches the targets via the existing tools and loops back) or to the unchanged confidence gate. The first retrieval pass (`investigate`) is untouched.

**Tech Stack:** Python 3.12, LangGraph `StateGraph`, the project's `HybridRouter` (mock-LLM seam for tests), `tvastr.analysis._jsonutil.extract_json`.

## Global Constraints

- The loop must NEVER crash or block a run — every new branch degrades safely to the confidence gate with whatever `code_files` exist. Mirror the existing graceful-degradation style (`search_codebase` → `[]`, `retrieve_code_files` → `{}` on miss).
- Loop is provably bounded: hard cap **`_MAX_EXPANSIONS = 2`** (≤3 reasoning passes), stop when `need_more_context` is false, stop when no *new* targets remain, and fail-safe to "done" on unparseable reasoning output.
- Accumulated context is capped at **`_MAX_CONTEXT_FILES = 12`**.
- The confidence gate is UNCHANGED (`_EVIDENCE_CONFIDENCE = {"stack_trace": 0.8, "search": 0.6}`, default 0.3; threshold `ctx.min_confidence`). `evidence_source` stays whatever `investigate` set.
- Backward compatible: when the LLM returns no JSON directive (e.g. the `MockClaudeClient` prose response), behavior is a single pass exactly as today. The existing suite must stay green.
- PII boundary unchanged: all retrieval/reasoning still flows through `ctx.router.run` redaction; add no cloud entry point that bypasses it.
- Reuse the existing tools only: `search_codebase(ctx, query, *, limit=5) -> list[str]`, `retrieve_code_files(ctx, paths, *, max_files=5) -> dict[str,str]`, `format_code_for_prompt(files) -> str`. No new tools, no new deps.
- TDD, frequent commits. Commit message footer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/agent/state.py` (modify) | 4 new `AgentState` keys |
| `src/tvastr/agent/graph.py` (modify) | `_parse_reasoning` helper; `_reason_root_cause` emits directives; new `_expand_context` node; `_should_expand` + `_after_reason` routing; loop wiring; docstring diagram |
| `tests/conftest.py` (modify) | `scripted_reasoning` fixture (drives the loop deterministically) |
| `tests/test_agent_reasoning.py` (create) | `_parse_reasoning` unit + reason-node directive tests |
| `tests/test_agent_expand.py` (create) | `_expand_context` unit + loop integration + degradation tests |

---

## Task 1: Reasoning emits retrieval directives

**Files:**
- Modify: `src/tvastr/agent/state.py` (add `need_more_context`, `next_targets` keys)
- Modify: `src/tvastr/agent/graph.py` (`_parse_reasoning` helper + `_reason_root_cause`)
- Modify: `tests/conftest.py` (add `scripted_reasoning` fixture)
- Test: `tests/test_agent_reasoning.py`

**Interfaces:**
- Produces:
  - `_parse_reasoning(text: str) -> tuple[str, bool, dict]` — returns `(summary, need_more_context, next_targets)` where `next_targets` is `{"queries": list[str], "paths": list[str]}`.
  - `_reason_root_cause` now returns the extra state keys `need_more_context: bool` and `next_targets: dict`.
  - `tests/conftest.py::scripted_reasoning` fixture — a factory `make(settings, root_cause_texts: list[str]) -> router` whose `.run()` returns the next scripted text for `TaskType.ROOT_CAUSE` calls and delegates every other task to the real mock router. Used by Tasks 1 and 3.

- [ ] **Step 1: Add the two state keys**

In `src/tvastr/agent/state.py`, inside `class AgentState(TypedDict, total=False)`, add after `evidence_source`:

```python
    need_more_context: bool  # reasoning asked for another retrieval round
    next_targets: dict  # {"queries": list[str], "paths": list[str]} from reasoning
```

- [ ] **Step 2: Add the `scripted_reasoning` fixture to conftest**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def scripted_reasoning():
    """Factory for a router that returns scripted ROOT_CAUSE responses (to drive
    the iterative-retrieval loop) and delegates every other task to the real mock
    router. Usage: router = scripted_reasoning(settings, [text_pass0, text_pass1])."""
    from tvastr.domain import RoutingDecision, Sensitivity
    from tvastr.llm.base import LLMResponse
    from tvastr.llm.router import TaskType, build_router

    def _make(settings, root_cause_texts):
        real = build_router(settings)
        texts = list(root_cause_texts)

        class _Router:
            def run(self, task, prompt, **kwargs):
                if task == TaskType.ROOT_CAUSE and texts:
                    text = texts.pop(0)
                    decision = RoutingDecision(
                        task=task.value, target="cloud", model="mock",
                        sensitivity=Sensitivity.INTERNAL, reason="scripted",
                    )
                    return LLMResponse(text=text, model="mock", target="cloud", mocked=True), decision
                return real.run(task, prompt, **kwargs)

        return _Router()

    return _make
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_agent_reasoning.py`:

```python
"""Tests for reasoning-emitted retrieval directives."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent, _parse_reasoning
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def test_parse_reasoning_extracts_directives():
    text = (
        'Some preamble. {"root_cause": "field renamed", "need_more_context": true, '
        '"next_targets": {"queries": ["candidates_token_count"], "paths": ["a/b.py"]}}'
    )
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == "field renamed"
    assert need_more is True
    assert targets == {"queries": ["candidates_token_count"], "paths": ["a/b.py"]}


def test_parse_reasoning_falls_back_on_prose():
    text = "The root cause is a contract mismatch between components."
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == text
    assert need_more is False
    assert targets == {"queries": [], "paths": []}


def test_parse_reasoning_handles_partial_json():
    # JSON present but missing the root_cause key -> treat as prose/fallback.
    text = '{"need_more_context": true, "next_targets": {"queries": ["x"]}}'
    summary, need_more, targets = _parse_reasoning(text)
    assert summary == text
    assert need_more is False
    assert targets == {"queries": [], "paths": []}


def _ctx(sink, router):
    settings = Settings(use_mocks=True, audit_backend="memory")
    return AgentContext(
        router=router, code_host=MockGitHubClient(),
        notifier=build_notifier(settings), event_sink=sink, run_id="t",
    )


def _pattern():
    return FailurePattern(
        fingerprint="f", title="No token count for Gemini 2.5",
        representative_message="UnexpectedBehavior: No token count for Gemini 2.5",
    )


def test_reason_node_writes_directives(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = scripted_reasoning(
        settings,
        ['{"root_cause": "parser ignores renamed field", "need_more_context": true, '
         '"next_targets": {"queries": ["candidates_token_count"], "paths": []}}'],
    )
    agent = RemediationAgent(_ctx(ListEventSink(), router))
    out = agent._reason_root_cause({"pattern": _pattern(), "suspected_files": [], "code_context": ""})
    assert out["need_more_context"] is True
    assert out["next_targets"]["queries"] == ["candidates_token_count"]
    assert out["root_cause"].summary == "parser ignores renamed field"


def test_reason_node_prose_response_is_single_pass():
    # The default MockClaudeClient returns prose -> no directives -> need_more False.
    settings = Settings(use_mocks=True, audit_backend="memory")
    agent = RemediationAgent(_ctx(ListEventSink(), build_router(settings)))
    out = agent._reason_root_cause({"pattern": _pattern(), "suspected_files": [], "code_context": ""})
    assert out["need_more_context"] is False
    assert out["next_targets"] == {"queries": [], "paths": []}
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_reasoning.py -v`
Expected: FAIL — `ImportError: cannot import name '_parse_reasoning'` (and the fixture-using tests error on the missing function).

- [ ] **Step 5: Implement `_parse_reasoning`**

In `src/tvastr/agent/graph.py`, add the import near the top (with the other `tvastr.analysis` import):

```python
from tvastr.analysis._jsonutil import extract_json
```

Add this module-level function after `_search_query_from_message`:

```python
def _parse_reasoning(text: str) -> tuple[str, bool, dict]:
    """Split a reasoning response into (summary, need_more_context, next_targets).

    The reasoning LLM is asked for JSON {"root_cause", "need_more_context",
    "next_targets": {"queries", "paths"}}. If no such object is present (e.g. a
    prose mock response), fall back to (text, False, empty) — a single pass,
    exactly the pre-loop behavior.
    """
    empty = {"queries": [], "paths": []}
    parsed = extract_json(text)
    if not parsed or "root_cause" not in parsed:
        return text, False, empty
    summary = str(parsed.get("root_cause") or text)
    need_more = bool(parsed.get("need_more_context", False))
    targets = parsed.get("next_targets") or {}
    queries = [str(q) for q in (targets.get("queries") or []) if q]
    paths = [str(p) for p in (targets.get("paths") or []) if p]
    return summary, need_more, {"queries": queries, "paths": paths}
```

- [ ] **Step 6: Rewrite `_reason_root_cause` to request JSON and emit directives**

Replace the body of `_reason_root_cause` in `src/tvastr/agent/graph.py` with:

```python
    def _reason_root_cause(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        suspected = state.get("suspected_files", [])
        self._emit("agent.node.start", "reason_root_cause", pattern=pattern.fingerprint)
        prompt = (
            f"A recurring failure has been detected ({pattern.count} occurrences).\n"
            f"Title: {pattern.title}\n"
            f"Message: {pattern.representative_message}\n"
            f"Suspected files: {', '.join(suspected) or 'unknown'}\n\n"
            f"Code context:\n{state.get('code_context') or '(none)'}\n\n"
            "Diagnose the root cause. If the code context is insufficient to pinpoint "
            "the bug (the real fault may be in a file you have not seen yet), say so and "
            "propose where to look next.\n\n"
            'Respond ONLY with JSON: {"root_cause": "2-3 sentence explanation", '
            '"need_more_context": true|false, "next_targets": '
            '{"queries": ["code-search terms"], "paths": ["repo/file/paths"]}}. '
            "Set need_more_context to false and leave next_targets empty when the "
            "current context is enough to write the fix."
        )
        response, decision = self.ctx.router.run(
            TaskType.ROOT_CAUSE, prompt, sensitivity=pattern.sensitivity
        )
        summary, need_more, next_targets = _parse_reasoning(response.text)
        # Confidence is unchanged: it tracks how the FIRST evidence was found
        # (stack trace 0.8 / search 0.6 / none 0.3), not the loop.
        confidence = _EVIDENCE_CONFIDENCE.get(state.get("evidence_source", "none"), 0.3)
        root_cause = RootCause(
            pattern_id=pattern.id,
            summary=summary,
            suspected_files=suspected,
            confidence=confidence,
            reasoning=response.text,
        )
        self._emit(
            "agent.node.end",
            "reason_root_cause",
            confidence=confidence,
            summary=root_cause.summary,
            need_more_context=need_more,
        )
        return {
            "root_cause": root_cause,
            "need_more_context": need_more,
            "next_targets": next_targets,
            "routing": _append_routing(state, decision),
        }
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_reasoning.py -v`
Expected: PASS (6 tests).

- [ ] **Step 8: Run the existing agent suite (regression)**

Run: `uv run pytest tests/test_agent_investigate.py tests/test_agent_compare.py -q`
Expected: PASS — prose reasoning still yields a valid `root_cause`; nothing references the new keys yet.

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/agent/state.py src/tvastr/agent/graph.py tests/conftest.py tests/test_agent_reasoning.py
git commit -m "feat(agent): reason_root_cause emits retrieval directives"
```

---

## Task 2: `expand_context` node + investigate seeds dedup set

**Files:**
- Modify: `src/tvastr/agent/state.py` (add `retrieval_iterations`, `retrieved_paths`)
- Modify: `src/tvastr/agent/graph.py` (`_expand_context` node, constants; `_investigate` seeds `retrieved_paths`)
- Test: `tests/test_agent_expand.py`

**Interfaces:**
- Consumes: `next_targets` (from Task 1), `code_files`/`code_context`, `suspected_files` (existing).
- Produces:
  - module constants `_MAX_EXPANSIONS = 2`, `_MAX_CONTEXT_FILES = 12` in `graph.py`.
  - `RemediationAgent._expand_context(state) -> dict` returning merged `code_files`, `code_context`, updated `retrieved_paths` (a `set[str]`), incremented `retrieval_iterations`.
  - `_investigate` additionally returns `retrieved_paths: set[str]` seeded with its suspected paths.

- [ ] **Step 1: Add the two state keys**

In `src/tvastr/agent/state.py`, add after the keys from Task 1:

```python
    retrieval_iterations: int  # number of expand_context rounds run
    retrieved_paths: set[str]  # paths fetched + queries issued, for cross-round dedup
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_agent_expand.py`:

```python
"""Tests for the expand_context node (iterative retrieval)."""

from __future__ import annotations

import pytest

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.config import Settings
from tvastr.domain import FailurePattern
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient


class _FakeHost:
    """Code host with scripted search + file contents, for deterministic retrieval."""

    def __init__(self, search_map=None, files=None):
        self.search_map = search_map or {}      # query -> [paths]
        self.files = files or {}                 # path -> content
        self.fetched: list[str] = []

    def search_code(self, query, *, limit=5):
        return list(self.search_map.get(query, []))[:limit]

    def get_file(self, path):
        self.fetched.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def open_pull_request(self, draft):  # unused here
        raise NotImplementedError


def _agent(host, sink=None):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=None, code_host=host, notifier=build_notifier(settings),
        event_sink=sink or ListEventSink(), run_id="t",
    )
    return RemediationAgent(ctx)


def _pattern():
    return FailurePattern(fingerprint="f", title="t", representative_message="m")


def test_expand_executes_queries_and_paths_and_merges():
    host = _FakeHost(
        search_map={"candidates_token_count": ["core/token_counting.py"]},
        files={"core/token_counting.py": "code A", "llms/utils.py": "code B"},
    )
    agent = _agent(host)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": ["candidates_token_count"], "paths": ["llms/utils.py"]},
        "code_files": {}, "retrieved_paths": set(), "retrieval_iterations": 0,
    })
    assert out["code_files"] == {"core/token_counting.py": "code A", "llms/utils.py": "code B"}
    assert out["retrieval_iterations"] == 1
    assert "core/token_counting.py" in out["code_context"]


def test_expand_dedups_already_retrieved():
    host = _FakeHost(files={"a.py": "x"})
    agent = _agent(host)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": ["a.py"]},
        "code_files": {"a.py": "x"}, "retrieved_paths": {"a.py"}, "retrieval_iterations": 0,
    })
    assert host.fetched == []           # already had it; nothing re-fetched
    assert out["code_files"] == {"a.py": "x"}


def test_expand_caps_total_files():
    files = {f"f{i}.py": "c" for i in range(20)}
    host = _FakeHost(files=files)
    agent = _agent(host)
    existing = {f"e{i}.py": "c" for i in range(10)}  # already 10 in context
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": list(files)},
        "code_files": dict(existing), "retrieved_paths": set(existing), "retrieval_iterations": 0,
    })
    assert len(out["code_files"]) == 12   # _MAX_CONTEXT_FILES


def test_expand_records_missing_path_and_survives():
    host = _FakeHost(files={})            # every get_file raises
    sink = ListEventSink()
    agent = _agent(host, sink)
    out = agent._expand_context({
        "pattern": _pattern(),
        "next_targets": {"queries": [], "paths": ["ghost.py"]},
        "code_files": {}, "retrieved_paths": set(), "retrieval_iterations": 0,
    })
    assert out["code_files"] == {}        # no crash; nothing added
    ends = [e for e in sink.events if e.type == "agent.node.end" and e.step == "expand_context"]
    assert ends and ends[-1].payload["missing_paths"] == ["ghost.py"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_expand.py -v`
Expected: FAIL — `AttributeError: 'RemediationAgent' object has no attribute '_expand_context'`.

- [ ] **Step 4: Add the constants and the `_expand_context` node**

In `src/tvastr/agent/graph.py`, add module constants near `_EVIDENCE_CONFIDENCE`:

```python
_MAX_EXPANSIONS = 2  # extra retrieval rounds beyond the first investigate pass
_MAX_CONTEXT_FILES = 12  # cap on accumulated code_files
```

Add this method to `RemediationAgent` (place it right after `_reason_root_cause`):

```python
    def _expand_context(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        iteration = state.get("retrieval_iterations", 0) + 1
        targets = state.get("next_targets") or {"queries": [], "paths": []}
        seen = set(state.get("retrieved_paths", set()))
        code_files = dict(state.get("code_files", {}))
        self._emit("agent.node.start", "expand_context", pattern=pattern.fingerprint, iteration=iteration)
        try:
            # New paths come from fresh searches + the LLM's explicit path picks.
            new_paths: list[str] = []
            for query in targets["queries"]:
                if query in seen:
                    continue
                seen.add(query)
                hits = search_codebase(self.ctx, query)
                self._emit("tool.call", "search_codebase", query=query, paths=hits)
                for hit in hits:
                    if hit not in seen and hit not in code_files and hit not in new_paths:
                        new_paths.append(hit)
            for path in targets["paths"]:
                if path not in seen and path not in code_files and path not in new_paths:
                    new_paths.append(path)

            budget = max(0, _MAX_CONTEXT_FILES - len(code_files))
            to_fetch = new_paths[:budget]
            fetched = retrieve_code_files(self.ctx, to_fetch, max_files=budget) if to_fetch else {}
            missing = [p for p in to_fetch if p not in fetched]
            self._emit(
                "tool.call", "retrieve_code_files",
                requested=len(to_fetch), retrieved=len(fetched), paths=list(fetched.keys()),
            )
            code_files.update(fetched)
            seen.update(to_fetch)
            self._emit(
                "agent.node.end", "expand_context",
                files_added=len(fetched), missing_paths=missing, total_files=len(code_files),
            )
            return {
                "code_files": code_files,
                "code_context": format_code_for_prompt(code_files),
                "retrieved_paths": seen,
                "retrieval_iterations": iteration,
            }
        except Exception as exc:  # never crash the run — bump the counter so the cap stops us
            log.warning("agent.expand_context.failed", error=str(exc))
            self._emit("agent.node.end", "expand_context", files_added=0, missing_paths=[], error=str(exc))
            return {"retrieval_iterations": iteration}
```

- [ ] **Step 5: Seed `retrieved_paths` in `_investigate`**

In `_investigate`'s return dict (end of the method), add the `retrieved_paths` key:

```python
        return {
            "suspected_files": suspected,
            "evidence_source": evidence_source,
            "code_files": code_files,
            "code_context": format_code_for_prompt(code_files),
            "retrieved_paths": set(suspected),
        }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_expand.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add src/tvastr/agent/state.py src/tvastr/agent/graph.py tests/test_agent_expand.py
git commit -m "feat(agent): expand_context node fetches reasoning's retrieval targets"
```

---

## Task 3: Wire the loop (routing + edges) and validate end to end

**Files:**
- Modify: `src/tvastr/agent/graph.py` (`_should_expand`, `_after_reason`, `_build` edges, module docstring)
- Test: `tests/test_agent_expand.py` (append integration tests)

**Interfaces:**
- Consumes: `_should_expand`/`_confidence_gate` state keys (`need_more_context`, `retrieval_iterations`, `next_targets`, `retrieved_paths`, `root_cause`); the `scripted_reasoning` fixture from Task 1.
- Produces: `_after_reason(state) -> "expand" | "act" | "skip"` wired as the single conditional edge out of `reason_root_cause`; `expand_context → reason_root_cause` loop edge.

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_agent_expand.py`:

```python
def _loop_agent(host, router, sink):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=router, code_host=host, notifier=build_notifier(settings),
        event_sink=sink, run_id="t",
    )
    return RemediationAgent(ctx)


def _directive(need_more, queries=(), paths=()):
    import json
    return json.dumps({
        "root_cause": "analysis", "need_more_context": need_more,
        "next_targets": {"queries": list(queries), "paths": list(paths)},
    })


def test_loop_converges_then_proceeds(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = _FakeHost(
        search_map={"candidates_token_count": ["core/token_counting.py"]},
        files={"core/token_counting.py": "real parser code"},
    )
    # Pass 0 asks for more; pass 1 is satisfied.
    router = scripted_reasoning(settings, [
        _directive(True, queries=["candidates_token_count"]),
        _directive(False),
    ])
    sink = ListEventSink()
    agent = _loop_agent(host, router, sink)
    pattern = FailurePattern(
        fingerprint="f", title="No token count",
        representative_message="UnexpectedBehavior: No token count",
    )
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [e for e in sink.events if e.type == "agent.node.start" and e.step == "expand_context"]
    assert len(expands) == 1                       # exactly one extra round
    assert "core/token_counting.py" in host.fetched  # it reached the real file


def test_loop_stops_at_hard_cap(scripted_reasoning):
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = _FakeHost(
        search_map={"q": ["a.py", "b.py", "c.py", "d.py"]},
        files={p: "c" for p in ["a.py", "b.py", "c.py", "d.py"]},
    )
    # Always asks for more, with a NEW query each round so dedup never stops it.
    router = scripted_reasoning(settings, [
        _directive(True, queries=["q"]),
        _directive(True, paths=["b.py"]),
        _directive(True, paths=["c.py"]),
        _directive(True, paths=["d.py"]),
    ])
    sink = ListEventSink()
    agent = _loop_agent(host, router, sink)
    pattern = FailurePattern(fingerprint="f", title="t", representative_message="m")
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [e for e in sink.events if e.type == "agent.node.start" and e.step == "expand_context"]
    assert len(expands) == 2                        # _MAX_EXPANSIONS, no infinite loop


def test_loop_back_compat_single_pass_on_prose():
    # Real mock router returns prose -> need_more False -> no expansion at all.
    from tvastr.llm.router import build_router
    settings = Settings(use_mocks=True, audit_backend="memory")
    host = MockGitHubClient()
    sink = ListEventSink()
    agent = _loop_agent(host, build_router(settings), sink)
    pattern = FailurePattern(
        fingerprint="f", title="ModuleNotFoundError in x",
        representative_message="ModuleNotFoundError: no mod",
    )
    agent.run({"pattern": pattern, "sample_events": []})
    expands = [e for e in sink.events if e.step == "expand_context"]
    assert expands == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_expand.py -k loop -v`
Expected: FAIL — the graph has no `expand_context` node wired, so `agent.run` never expands (`len(expands)` assertions fail; converge test sees 0 expands).

- [ ] **Step 3: Add `_should_expand` and `_after_reason`**

In `src/tvastr/agent/graph.py`, add these methods to `RemediationAgent` (after `_confidence_gate`):

```python
    def _should_expand(self, state: AgentState) -> bool:
        if not state.get("need_more_context"):
            return False
        if state.get("retrieval_iterations", 0) >= _MAX_EXPANSIONS:
            return False
        targets = state.get("next_targets") or {"queries": [], "paths": []}
        seen = state.get("retrieved_paths", set())
        fresh = [t for t in (targets["queries"] + targets["paths"]) if t not in seen]
        return bool(fresh)

    def _after_reason(self, state: AgentState) -> str:
        """Route out of reasoning: loop to expand_context, or run the gate."""
        if self._should_expand(state):
            return "expand"
        return self._confidence_gate(state)
```

- [ ] **Step 4: Rewire the graph in `_build`**

In `_build`, register the new node and replace the `reason_root_cause` conditional edge. Change:

```python
        g.add_node("reason_root_cause", self._reason_root_cause)
        g.add_node("generate_fix", self._generate_fix)
```

to add the expand node:

```python
        g.add_node("reason_root_cause", self._reason_root_cause)
        g.add_node("expand_context", self._expand_context)
        g.add_node("generate_fix", self._generate_fix)
```

and replace:

```python
        g.add_conditional_edges(
            "reason_root_cause",
            self._confidence_gate,
            {"act": "generate_fix", "skip": "notify"},
        )
```

with:

```python
        g.add_conditional_edges(
            "reason_root_cause",
            self._after_reason,
            {"expand": "expand_context", "act": "generate_fix", "skip": "notify"},
        )
        g.add_edge("expand_context", "reason_root_cause")
```

- [ ] **Step 5: Update the module docstring flow diagram**

In the top-of-file docstring, replace the flow block with:

```
    START -> investigate -> reason_root_cause -> (confidence gate)
        high → generate_fix → compare_to_pr → draft_pr → open_pr → notify → END
        low  → notify (skipped) → END

    reason_root_cause may loop through expand_context (bounded to 2 extra
    rounds) to fetch code its own analysis asked for before the gate fires.

    compare_to_pr benchmarks the agent's fix against the upstream PR (the
    ground-truth oracle) when one was discovered; it emits benchmark.compared
    or benchmark.skipped and never blocks the act path.
```

- [ ] **Step 6: Run the loop tests to verify they pass**

Run: `uv run pytest tests/test_agent_expand.py -v`
Expected: PASS (all unit + integration tests).

- [ ] **Step 7: Full suite + lint (regression)**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean. (The default mock path is single-pass, so prior agent/pipeline tests are unaffected.)

- [ ] **Step 8: Commit**

```bash
git add src/tvastr/agent/graph.py tests/test_agent_expand.py
git commit -m "feat(agent): wire the bounded reason<->expand_context retrieval loop"
```

---

## Final verification (after all tasks)

- [ ] Full suite + lint:

```bash
uv run pytest -q && uv run ruff check src tests
```
Expected: all green, lint clean.

- [ ] **Live spot-check (success metric).** With `TVASTR_USE_MOCKS=false` and tokens present, run issue #19293 through `/app` (or `curl /api/run`) and confirm the timeline shows an `expand_context` round fetching the gemini integration source, and the `benchmark.compared` verdict moves off `divergent` (toward `match`/`partial`). This is the spec's definition of success; record the before/after verdict.

## Self-Review (completed by author)

- **Spec coverage:** loop topology (Tasks 1–3), LLM-proposed targets + `need_more_context`/`next_targets` schema (Task 1), bounded loop with all four stop conditions — LLM-done + cap + no-new-targets + fail-safe (Tasks 1–3), reuse existing tools (Task 2), gate unchanged (Task 1 keeps `_EVIDENCE_CONFIDENCE`/`_confidence_gate`), graceful degradation (Task 2 try/except + Task 3 routing), `_MAX_CONTEXT_FILES`=12 / `_MAX_EXPANSIONS`=2 (Tasks 2–3), back-compat single pass (Task 1 prose fallback + Task 3 test), PII boundary unchanged (no new cloud entry point). Live success metric in Final verification.
- **Placeholder scan:** none — every code/test step shows complete code.
- **Type consistency:** `_parse_reasoning -> (str, bool, dict)`, `next_targets={"queries","paths"}`, `retrieved_paths: set[str]`, `_after_reason -> "expand"|"act"|"skip"` used identically across tasks; constants `_MAX_EXPANSIONS`/`_MAX_CONTEXT_FILES` defined in Task 2, consumed in Task 3.
