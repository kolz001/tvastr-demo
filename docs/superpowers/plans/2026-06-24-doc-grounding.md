# Documentation Grounding (`ground_root_cause`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `ground_root_cause` node that validates/corrects the agent's diagnosis against authoritative external docs (via Anthropic's built-in `web_search`) before `generate_fix`.

**Architecture:** A new node on the act path (gate `"act"` → `ground_root_cause` → `generate_fix`) makes one cloud call via a new `TaskType.DOC_GROUNDING` with Anthropic's server-side `web_search` tool offered. Claude self-decides whether to search; it returns a corrected root-cause summary + citation URLs. Gated off by default and a no-op in mock/no-key; never crashes the run.

**Tech Stack:** Python 3.12, LangGraph, the project `HybridRouter`, the `anthropic` SDK (server-side `web_search_20250305` tool), Pydantic.

## Global Constraints

- Documentation source is **Anthropic's built-in server-side `web_search` tool** (`{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}`). No new keys, no agent-side URL fetching.
- The node makes **one** cloud call and routes unconditionally to `generate_fix` — no loop.
- **Gated three ways** — skip (root_cause unchanged) when: mock mode, no Anthropic key, or `TVASTR_DOC_GROUNDING` off. The single gate signal the node reads is `self.ctx.doc_grounding` (a bool the pipeline computes from all three).
- **Default off:** `doc_grounding: bool = False` (env `TVASTR_DOC_GROUNDING`), consistent with `pii_local_model`/`auto_analyze_prs`.
- **Never crash the run:** the call is wrapped in try/except → on any failure emit `doc.skipped {reason}` and return `{}` (root_cause unchanged); `generate_fix` proceeds.
- **Event semantics:** `doc.skipped` = no diagnosis produced (gated or errored). `doc.grounded` = call returned; payload carries `searched: bool`, `changed: bool`, `sources: list[str]`.
- **PII unchanged:** `DOC_GROUNDING` is a cloud task, so the router redacts the prompt before the call exactly like every other cloud task.
- **Back-compat:** mock mode → node skips → existing suite stays green.
- TDD, frequent commits. Commit footer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/llm/base.py` (modify) | `LLMResponse.sources` field |
| `src/tvastr/llm/router.py` (modify) | `TaskType.DOC_GROUNDING`; `run(..., web_search=False)` passes through to the cloud client |
| `src/tvastr/llm/claude.py` (modify) | `ClaudeClient.complete(..., web_search=False)` adds the tool + extracts citations; `MockClaudeClient.complete` accepts+ignores the param |
| `src/tvastr/config.py` (modify) | `doc_grounding` flag |
| `src/tvastr/agent/context.py` (modify) | `AgentContext.doc_grounding` field |
| `src/tvastr/pipeline.py` (modify) | compute `doc_grounding` from settings and pass to `AgentContext` |
| `src/tvastr/events.py` (modify) | `doc.grounded` / `doc.skipped` event types |
| `src/tvastr/agent/state.py` (modify) | `doc_sources` key |
| `src/tvastr/agent/graph.py` (modify) | `_ground_root_cause` node + gate rewire |
| `src/tvastr/api/templates/app.html` (modify) | `summarize()` cases |
| `tests/test_llm_web_search.py` (create) | client + router web_search mechanism |
| `tests/test_agent_grounding.py` (create) | node skip/ground/degradation + graph wiring |

---

## Task 1: web_search mechanism (LLMResponse, router, ClaudeClient)

**Files:**
- Modify: `src/tvastr/llm/base.py`, `src/tvastr/llm/router.py`, `src/tvastr/llm/claude.py`
- Test: `tests/test_llm_web_search.py`

**Interfaces:**
- Produces:
  - `LLMResponse.sources: list[str]` (default empty).
  - `TaskType.DOC_GROUNDING = "doc_grounding"` (a cloud task).
  - `HybridRouter.run(task, prompt, *, sensitivity=..., system=None, web_search=False)` — when `web_search` is true AND the task routes to cloud, the cloud client receives `web_search=True`.
  - `ClaudeClient.complete(prompt, *, system=None, web_search=False) -> LLMResponse` — with `web_search`, adds the `web_search_20250305` tool and returns `LLMResponse` with `sources` populated from citation URLs.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm_web_search.py`:

```python
"""Tests for the Anthropic web_search wiring."""

from __future__ import annotations

import types

from tvastr.llm.base import LLMResponse
from tvastr.llm.claude import ClaudeClient
from tvastr.llm.router import HybridRouter, TaskType


def test_llmresponse_sources_defaults_empty():
    r = LLMResponse(text="x", model="m", target="cloud")
    assert r.sources == []


def test_doc_grounding_is_a_cloud_task():
    # Not in the local set, so it routes to cloud (and gets redacted).
    assert TaskType.DOC_GROUNDING.value == "doc_grounding"


def test_claude_client_web_search_adds_tool_and_extracts_citations(monkeypatch):
    captured = {}

    class _Cit:
        def __init__(self, url):
            self.url = url

    class _Block:
        def __init__(self, text, citations):
            self.type = "text"
            self.text = text
            self.citations = citations

    class _Msg:
        content = [_Block("grounded answer", [_Cit("https://docs.example/api")])]

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Msg()

    class _Anthropic:
        def __init__(self, api_key):
            self.messages = _Messages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)

    client = ClaudeClient(api_key="k", model="claude-x")
    resp = client.complete("diagnose this", system="sys", web_search=True)

    assert resp.text == "grounded answer"
    assert resp.sources == ["https://docs.example/api"]
    tools = captured.get("tools")
    assert tools and tools[0]["type"] == "web_search_20250305"
    assert tools[0]["max_uses"] == 3


def test_claude_client_without_web_search_passes_no_tools(monkeypatch):
    captured = {}

    class _Block:
        type = "text"
        text = "plain"
        citations = None

    class _Msg:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Msg()

    class _Anthropic:
        def __init__(self, api_key):
            self.messages = _Messages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)

    resp = ClaudeClient(api_key="k", model="m").complete("hi")
    assert resp.text == "plain"
    assert resp.sources == []
    assert "tools" not in captured  # no tools key when web_search is off


def test_router_passes_web_search_to_cloud_client_only():
    class _CloudClient:
        target = "cloud"
        model = "cloud-m"

        def __init__(self):
            self.calls = []

        def complete(self, prompt, *, system=None, web_search=False):
            self.calls.append(web_search)
            return LLMResponse(text="ok", model=self.model, target="cloud")

    class _LocalClient:
        target = "local"
        model = "local-m"

        def complete(self, prompt, *, system=None):
            return LLMResponse(text="ok", model=self.model, target="local")

    cloud = _CloudClient()
    router = HybridRouter(local=_LocalClient(), cloud=cloud)
    router.run(TaskType.DOC_GROUNDING, "p", web_search=True)
    assert cloud.calls == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm_web_search.py -v`
Expected: FAIL — `LLMResponse` has no `sources`; `TaskType.DOC_GROUNDING` undefined; `complete()` rejects `web_search`.

- [ ] **Step 3: Add `sources` to `LLMResponse`**

In `src/tvastr/llm/base.py`, update the imports and model:

```python
from pydantic import BaseModel, Field
```

```python
class LLMResponse(BaseModel):
    text: str
    model: str
    target: str  # "local" | "cloud"
    mocked: bool = False
    sources: list[str] = Field(default_factory=list)  # citation URLs (web_search)
```

- [ ] **Step 4: Add the `DOC_GROUNDING` task type and the `web_search` router param**

In `src/tvastr/llm/router.py`, add to `TaskType`:

```python
    FIX_COMPARISON = "fix_comparison"
    DOC_GROUNDING = "doc_grounding"
```

Change the `run` signature to accept `web_search` and pass it to the cloud client only. Replace the signature and the `client.complete(...)` call:

```python
    def run(
        self,
        task: TaskType,
        prompt: str,
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        system: str | None = None,
        web_search: bool = False,
    ) -> tuple[LLMResponse, RoutingDecision]:
```

Find `response = client.complete(payload, system=system)` and replace with:

```python
        extra = {"web_search": web_search} if (target == "cloud" and web_search) else {}
        response = client.complete(payload, system=system, **extra)
```

- [ ] **Step 5: Add `web_search` to the Claude clients**

In `src/tvastr/llm/claude.py`, add a module constant and rewrite `ClaudeClient.complete`:

```python
_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
```

```python
    def complete(
        self, prompt: str, *, system: str | None = None, web_search: bool = False
    ) -> LLMResponse:
        import anthropic  # lazy import: only needed when not mocking

        client = anthropic.Anthropic(api_key=self.api_key)
        log.info("llm.cloud.complete", model=self.model, web_search=web_search)
        kwargs: dict = dict(
            model=self.model,
            max_tokens=2048,
            system=system or "You are a senior software engineer fixing production bugs.",
            messages=[{"role": "user", "content": prompt}],
        )
        if web_search:
            kwargs["tools"] = [_WEB_SEARCH_TOOL]
        message = client.messages.create(**kwargs)

        text_parts: list[str] = []
        sources: list[str] = []
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            for cit in getattr(block, "citations", None) or []:
                url = getattr(cit, "url", None)
                if url and url not in sources:
                    sources.append(url)
        return LLMResponse(
            text="".join(text_parts), model=self.model, target=self.target, sources=sources
        )
```

Update `MockClaudeClient.complete` to accept and ignore the param (so the router can pass it uniformly in non-mock-only paths):

```python
    def complete(
        self, prompt: str, *, system: str | None = None, web_search: bool = False
    ) -> LLMResponse:
```

(The body is unchanged; `web_search` is ignored — the mock never searches.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm_web_search.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Regression — router/llm suite**

Run: `uv run pytest tests/test_router.py -q`
Expected: PASS (existing router tests unaffected — `web_search` defaults false).

- [ ] **Step 8: Commit**

```bash
git add src/tvastr/llm/base.py src/tvastr/llm/router.py src/tvastr/llm/claude.py tests/test_llm_web_search.py
git commit -m "feat(llm): web_search-enabled DOC_GROUNDING task + citation extraction"
```

---

## Task 2: Config flag, AgentContext wiring, events, state key

**Files:**
- Modify: `src/tvastr/config.py`, `src/tvastr/agent/context.py`, `src/tvastr/pipeline.py`, `src/tvastr/events.py`, `src/tvastr/agent/state.py`
- Test: `tests/test_events.py` (extend), `tests/test_triage_api.py` is NOT touched

**Interfaces:**
- Consumes: nothing from Task 1 (independent scaffolding).
- Produces:
  - `Settings.doc_grounding: bool = False`.
  - `AgentContext.doc_grounding: bool = False`.
  - `pipeline.build_pipeline` sets `doc_grounding = settings.doc_grounding and not settings.use_mocks and bool(settings.anthropic_api_key)` on the `AgentContext`.
  - event types `"doc.grounded"`, `"doc.skipped"`.
  - `AgentState` key `doc_sources: list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events.py`:

```python
def test_doc_grounding_event_types_exist():
    from tvastr.events import EventType
    import typing
    args = typing.get_args(EventType)
    assert "doc.grounded" in args
    assert "doc.skipped" in args
```

Append to `tests/test_events.py` a config + context check (or place in a small new block):

```python
def test_doc_grounding_setting_defaults_false():
    from tvastr.config import Settings
    assert Settings(use_mocks=True).doc_grounding is False


def test_agent_context_doc_grounding_defaults_false():
    from tvastr.agent.context import AgentContext
    # Construct with the minimum required positional deps via keywords; defaults apply.
    from tvastr.config import Settings
    from tvastr.integrations import build_notifier
    from tvastr.integrations.github import MockGitHubClient
    from tvastr.llm.router import build_router
    s = Settings(use_mocks=True)
    ctx = AgentContext(
        router=build_router(s), code_host=MockGitHubClient(), notifier=build_notifier(s)
    )
    assert ctx.doc_grounding is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events.py -k "doc_grounding" -v`
Expected: FAIL — `doc.grounded` not in `EventType`; `Settings` has no `doc_grounding`; `AgentContext` has no `doc_grounding`.

- [ ] **Step 3: Add the config flag**

In `src/tvastr/config.py`, after the `pii_local_model` block (the `# --- PII redaction ---` section), add:

```python
    # --- Documentation grounding ---
    # When true (live mode + Anthropic key), the agent runs a web-search-grounded
    # diagnosis check before generating a fix. Off by default; no-op in mock mode.
    doc_grounding: bool = False
```

- [ ] **Step 4: Add the `AgentContext` field**

In `src/tvastr/agent/context.py`, add to the `AgentContext` dataclass (after `run_id`):

```python
    run_id: str | None = None
    doc_grounding: bool = False
```

- [ ] **Step 5: Wire it in the pipeline**

In `src/tvastr/pipeline.py`, the `AgentContext(...)` call (around line 229) — add the computed flag:

```python
    ctx = AgentContext(
        router=router,
        code_host=build_code_host(settings),
        notifier=build_notifier(settings),
        event_sink=sink,
        run_id=run_id,
        doc_grounding=settings.doc_grounding
        and not settings.use_mocks
        and bool(settings.anthropic_api_key),
    )
```

- [ ] **Step 6: Add the event types**

In `src/tvastr/events.py`, find the `EventType` Literal and add the two values immediately before `"error"`:

```python
    "doc.grounded",
    "doc.skipped",
    "error",
```

- [ ] **Step 7: Add the state key**

In `src/tvastr/agent/state.py`, add after `fix_comparison`:

```python
    doc_sources: list[str]  # citation URLs from documentation grounding
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_events.py -k "doc_grounding" -v`
Expected: PASS (3 tests).

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/config.py src/tvastr/agent/context.py src/tvastr/pipeline.py src/tvastr/events.py src/tvastr/agent/state.py tests/test_events.py
git commit -m "feat: doc_grounding flag, context wiring, events, state key"
```

---

## Task 3: `ground_root_cause` node + gate rewire + UI

**Files:**
- Modify: `src/tvastr/agent/graph.py`, `src/tvastr/api/templates/app.html`
- Test: `tests/test_agent_grounding.py`

**Interfaces:**
- Consumes: `TaskType.DOC_GROUNDING` + `web_search` (Task 1); `AgentContext.doc_grounding`, `doc.grounded`/`doc.skipped` events, `doc_sources` state key (Task 2).
- Produces: `RemediationAgent._ground_root_cause(state) -> dict`; gate `"act"` now routes to `ground_root_cause`, which edges to `generate_fix`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_grounding.py`:

```python
"""Tests for the ground_root_cause node."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.agent.graph import RemediationAgent
from tvastr.config import Settings
from tvastr.domain import FailurePattern, RootCause, RoutingDecision, Sensitivity
from tvastr.events import ListEventSink
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse


def _pattern():
    return FailurePattern(fingerprint="f", title="t", representative_message="m")


def _root_cause():
    return RootCause(
        pattern_id="p", summary="original diagnosis", suspected_files=["a.py"],
        confidence=0.6, reasoning="original diagnosis",
    )


class _GroundingRouter:
    """Returns a scripted grounded answer (with sources) for DOC_GROUNDING."""

    def __init__(self, text="corrected diagnosis", sources=("https://docs/x",)):
        self.text = text
        self.sources = list(sources)
        self.tasks = []

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None, web_search=False):
        self.tasks.append((task, web_search))
        resp = LLMResponse(text=self.text, model="mock", target="cloud", sources=self.sources)
        decision = RoutingDecision(
            task=task.value, target="cloud", model="mock",
            sensitivity=sensitivity, reason="scripted",
        )
        return resp, decision


def _agent(sink, router, *, doc_grounding):
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=router, code_host=MockGitHubClient(), notifier=build_notifier(settings),
        event_sink=sink, run_id="t", doc_grounding=doc_grounding,
    )
    return RemediationAgent(ctx)


def test_ground_skips_when_disabled():
    sink = ListEventSink()
    agent = _agent(sink, _GroundingRouter(), doc_grounding=False)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out == {}  # root_cause unchanged
    assert any(e.type == "doc.skipped" for e in sink.events)
    assert not any(e.type == "doc.grounded" for e in sink.events)


def test_ground_corrects_diagnosis_when_enabled():
    sink = ListEventSink()
    router = _GroundingRouter(text="Gemini 2.5 renamed the field to response_token_count")
    agent = _agent(sink, router, doc_grounding=True)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out["root_cause"].summary == "Gemini 2.5 renamed the field to response_token_count"
    assert out["doc_sources"] == ["https://docs/x"]
    assert router.tasks[0][1] is True  # web_search=True was passed
    grounded = [e for e in sink.events if e.type == "doc.grounded"]
    assert grounded and grounded[-1].payload["searched"] is True
    assert grounded[-1].payload["sources"] == ["https://docs/x"]


def test_ground_degrades_when_call_raises():
    class _BoomRouter:
        def run(self, *a, **k):
            raise RuntimeError("api down")

    sink = ListEventSink()
    agent = _agent(sink, _BoomRouter(), doc_grounding=True)
    out = agent._ground_root_cause({"pattern": _pattern(), "root_cause": _root_cause()})
    assert out == {}  # root_cause unchanged
    assert any(e.type == "doc.skipped" for e in sink.events)


def test_act_path_routes_through_ground_root_cause():
    # The compiled graph must wire gate 'act' -> ground_root_cause -> generate_fix.
    sink = ListEventSink()
    agent = _agent(sink, _GroundingRouter(), doc_grounding=True)
    # The mock code host + scripted router drive a full act-path run.
    agent.run({"pattern": _pattern(), "sample_events": []})
    steps = [e.step for e in sink.events if e.type == "agent.node.start"]
    assert "ground_root_cause" in steps
```

> NOTE: `test_act_path_routes_through_ground_root_cause` runs the real compiled graph. The `_GroundingRouter` returns the same scripted response for every task (root_cause, doc_grounding, fix_generation, …); the fix step will parse it loosely and may fall back, which is fine — we only assert the node ran. If the run errors on the scripted fix, narrow the assertion to call `agent._ground_root_cause` directly (covered by the other tests) and instead assert the wiring via `"ground_root_cause" in agent.graph.get_graph().nodes`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_grounding.py -v`
Expected: FAIL — `_ground_root_cause` does not exist; the graph has no `ground_root_cause` node.

- [ ] **Step 3: Add the `_ground_root_cause` node**

In `src/tvastr/agent/graph.py`, add a system constant near the other prompt constants:

```python
_DOC_GROUNDING_SYSTEM = (
    "You validate a bug diagnosis against authoritative external documentation."
)
```

Add this method to `RemediationAgent` (place it right after `_confidence_gate`):

```python
    def _ground_root_cause(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        root_cause = state.get("root_cause")
        if not self.ctx.doc_grounding or root_cause is None:
            self._emit("doc.skipped", "ground_root_cause", reason="grounding disabled")
            return {}
        self._emit("agent.node.start", "ground_root_cause", pattern=pattern.fingerprint)
        prompt = (
            f"Failure: {pattern.title}\n"
            f"Current diagnosis: {root_cause.summary}\n\n"
            f"Code context:\n{state.get('code_context') or '(none)'}\n\n"
            "Validate this diagnosis against authoritative external documentation. "
            "Use web_search ONLY if the root cause depends on third-party API/library "
            "behavior (e.g. a renamed field or changed return shape in a dependency). "
            "Return ONLY the corrected root-cause summary in 2-4 sentences; if the "
            "original was correct, restate it concisely."
        )
        try:
            response, decision = self.ctx.router.run(
                TaskType.DOC_GROUNDING,
                prompt,
                sensitivity=pattern.sensitivity,
                system=_DOC_GROUNDING_SYSTEM,
                web_search=True,
            )
        except Exception as exc:  # never crash the run
            log.warning("agent.ground_root_cause.failed", error=str(exc))
            self._emit("doc.skipped", "ground_root_cause", reason=f"grounding error: {exc}")
            return {}
        grounded_summary = response.text.strip() or root_cause.summary
        changed = grounded_summary != root_cause.summary
        new_root_cause = root_cause.model_copy(
            update={"summary": grounded_summary, "reasoning": response.text}
        )
        self._emit(
            "doc.grounded",
            "ground_root_cause",
            searched=bool(response.sources),
            changed=changed,
            sources=response.sources,
        )
        return {
            "root_cause": new_root_cause,
            "doc_sources": response.sources,
            "routing": _append_routing(state, decision),
        }
```

- [ ] **Step 4: Register the node and rewire the gate**

In `_build`, register the node (after `expand_context`):

```python
        g.add_node("expand_context", self._expand_context)
        g.add_node("ground_root_cause", self._ground_root_cause)
        g.add_node("generate_fix", self._generate_fix)
```

Change the gate's `"act"` target and add the edge to `generate_fix`. Replace:

```python
        g.add_conditional_edges(
            "reason_root_cause",
            self._after_reason,
            {"expand": "expand_context", "act": "generate_fix", "skip": "notify"},
        )
        g.add_edge("expand_context", "reason_root_cause")
        g.add_edge("generate_fix", "compare_to_pr")
```

with:

```python
        g.add_conditional_edges(
            "reason_root_cause",
            self._after_reason,
            {"expand": "expand_context", "act": "ground_root_cause", "skip": "notify"},
        )
        g.add_edge("expand_context", "reason_root_cause")
        g.add_edge("ground_root_cause", "generate_fix")
        g.add_edge("generate_fix", "compare_to_pr")
```

- [ ] **Step 5: Update the module docstring flow diagram**

In the top-of-file docstring flow block, change the high-confidence line to include the node:

```
        high → generate_fix → compare_to_pr → draft_pr → open_pr → notify → END
```

becomes

```
        high → ground_root_cause → generate_fix → compare_to_pr → draft_pr → open_pr → notify → END
```

and add a line below the existing notes:

```
    ground_root_cause (live + TVASTR_DOC_GROUNDING only) validates the diagnosis
    against external docs via web_search before the fix; it never blocks the run.
```

- [ ] **Step 6: Add the UI summary cases**

In `src/tvastr/api/templates/app.html`, in the `summarize(event)` switch, add after the `benchmark.skipped` case:

```javascript
    case "doc.grounded": return `${p.searched ? "searched docs" : "no search"}${p.changed ? " · diagnosis revised" : ""}${(p.sources||[]).length ? ` · ${p.sources.length} source(s)` : ""}`;
    case "doc.skipped":  return p.reason || "grounding skipped";
```

- [ ] **Step 7: Run the node tests to verify they pass**

Run: `uv run pytest tests/test_agent_grounding.py -v`
Expected: PASS (4 tests). If `test_act_path_routes_through_ground_root_cause` errors inside the scripted fix step, switch it to the node-presence assertion described in its NOTE.

- [ ] **Step 8: Full suite + lint (regression)**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean. (Mock mode → `doc_grounding` is false → node skips → prior agent/pipeline tests unaffected.)

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/agent/graph.py src/tvastr/api/templates/app.html tests/test_agent_grounding.py
git commit -m "feat(agent): ground_root_cause node — web-search-grounded diagnosis before fix"
```

---

## Final verification (after all tasks)

- [ ] Full suite + lint:

```bash
uv run pytest -q && uv run ruff check src tests
```
Expected: all green, lint clean.

- [ ] **Live spot-check (success metric).** With `TVASTR_USE_MOCKS=false`, an Anthropic key, and `TVASTR_DOC_GROUNDING=true`, run issue #19293 through `/app`. Confirm: the timeline shows a `doc.grounded` card with `searched: true` and ≥1 source, and the `benchmark.compared` verdict moves off `divergent` toward `partial`/`match`. Record the before/after verdict. This is the spec's definition of success.

## Self-Review (completed by author)

- **Spec coverage:** Anthropic web_search source (Task 1); `ground_root_cause` node on the act path before `generate_fix` (Task 3 gate rewire); always-run + Claude self-decides via the offered tool (Task 1 tool wiring + Task 3 prompt); three-way gating via `ctx.doc_grounding` computed from flag+mock+key (Task 2 pipeline wiring + Task 3 skip branch); default-off flag (Task 2); never-crash try/except (Task 3); `doc.grounded`/`doc.skipped` semantics with `searched`/`changed`/`sources` (Task 2 events + Task 3 emits); PII via router redaction (DOC_GROUNDING is a cloud task — Task 1); back-compat mock skip (Task 3 + Step 8); `doc_sources` state (Task 2); UI summary (Task 3). Live success metric in Final verification.
- **Placeholder scan:** none — every code/test step has complete code; the one conditional fallback (`test_act_path_...`) gives an explicit alternative assertion, not a TODO.
- **Type consistency:** `complete(..., web_search=False)` signature identical across `ClaudeClient`/`MockClaudeClient`/test stubs; `LLMResponse.sources: list[str]`; `run(..., web_search=False)`; `ctx.doc_grounding: bool`; `_ground_root_cause -> dict` writing `root_cause`/`doc_sources`/`routing`; gate map uses `"ground_root_cause"` consistently with the registered node and its `→ generate_fix` edge.
