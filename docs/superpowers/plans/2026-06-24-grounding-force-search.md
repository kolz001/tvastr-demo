# Force Web Search in Doc-Grounding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ClaudeClient.complete(web_search=True)` force the web search via `tool_choice` so doc-grounding deterministically consults docs instead of Claude self-deciding.

**Architecture:** When `web_search` is on, add `tool_choice={"type": "tool", "name": "web_search"}` to the Anthropic request alongside the tool. If the forced request raises (the API rejects forcing the server tool), retry once without `tool_choice` so grounding still runs. Single file: `src/tvastr/llm/claude.py`.

**Tech Stack:** Python 3.12, the `anthropic` SDK (server-side `web_search_20250305` tool + `tool_choice`).

## Global Constraints

- Force only when `web_search=True` (the grounding node is the sole caller). Non-`web_search` calls are unchanged — no `tool_choice`.
- `tool_choice` value is exactly `{"type": "tool", "name": "web_search"}`.
- **Fail-safe fallback:** if `messages.create` raises while `tool_choice` is set, retry once with `tool_choice` removed (tools still offered). A non-`web_search` call that raises must propagate unchanged.
- No new params, flags, or changes to the router, grounding node, state, config, or UI. Single file + its test.
- MANDATORY before committing: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/llm/claude.py` (modify) | force `tool_choice` when `web_search`; one-retry unforced fallback |
| `tests/test_llm_web_search.py` (modify) | force assertion, no-force assertion, fallback test |

---

## Task 1: Force the web_search tool with a fail-safe fallback

**Files:**
- Modify: `src/tvastr/llm/claude.py` (`ClaudeClient.complete`)
- Test: `tests/test_llm_web_search.py`

**Interfaces:**
- `ClaudeClient.complete(prompt, *, system=None, web_search=False)` — unchanged signature; when `web_search`, the request now also carries `tool_choice={"type": "tool", "name": "web_search"}`, with a one-retry unforced fallback on error.

- [ ] **Step 1: Update the two existing tests + add the fallback test**

In `tests/test_llm_web_search.py`:

(a) In `test_claude_client_web_search_adds_tool_and_extracts_citations`, after the existing `assert tools[0]["max_uses"] == 3` line, add:

```python
    assert captured["tool_choice"] == {"type": "tool", "name": "web_search"}
```

(b) In `test_claude_client_without_web_search_passes_no_tools`, after the existing `assert "tools" not in captured` line, add:

```python
    assert "tool_choice" not in captured  # only forced when web_search is on
```

(c) Add this new test (place after `test_claude_client_without_web_search_passes_no_tools`):

```python
def test_claude_client_web_search_falls_back_when_force_rejected(monkeypatch):
    calls: list[dict] = []

    class _Block:
        type = "text"
        text = "grounded after retry"
        citations = None

    class _Msg:
        def __init__(self):
            self.content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "tool_choice" in kwargs:
                raise RuntimeError("tool_choice not supported for server tool")
            return _Msg()

    class _Anthropic:
        def __init__(self, api_key):
            self.messages = _Messages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)

    resp = ClaudeClient(api_key="k", model="m").complete("p", web_search=True)
    assert resp.text == "grounded after retry"
    assert len(calls) == 2                  # forced attempt, then unforced retry
    assert "tool_choice" in calls[0]
    assert "tool_choice" not in calls[1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_llm_web_search.py -k "web_search" -v`
Expected: FAIL — the force assertion fails (`KeyError: 'tool_choice'` / not present), and the fallback test fails (the single `create` raises and propagates; no retry yet).

- [ ] **Step 3: Implement forcing + fallback in `complete`**

In `src/tvastr/llm/claude.py`, in `ClaudeClient.complete`, replace:

```python
        if web_search:
            kwargs["tools"] = [_WEB_SEARCH_TOOL]
        message = client.messages.create(**kwargs)
```

with:

```python
        if web_search:
            kwargs["tools"] = [_WEB_SEARCH_TOOL]
            kwargs["tool_choice"] = {"type": "tool", "name": "web_search"}
        try:
            message = client.messages.create(**kwargs)
        except Exception:
            if "tool_choice" not in kwargs:
                raise  # non-web_search failures propagate unchanged
            # Some API configs reject forcing the server-side web_search tool;
            # retry once offered (non-forced) so grounding still runs.
            log.warning("llm.cloud.web_search.force_rejected", model=self.model)
            kwargs.pop("tool_choice")
            message = client.messages.create(**kwargs)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm_web_search.py -v`
Expected: PASS (all — the three touched tests plus the rest of the file).

- [ ] **Step 5: Full suite + lint (regression)**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, "All checks passed!". (Mock mode ignores `web_search`; the grounding node is unchanged; only the live request shape differs.)

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/llm/claude.py tests/test_llm_web_search.py
git commit -m "feat(llm): force web_search via tool_choice with unforced fallback"
```

---

## Final verification (after the task)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With live mode + Anthropic key + `TVASTR_DOC_GROUNDING=true`, run #19293 a few times. Confirm `doc.grounded` reliably shows `searched: true` with ≥1 source on every run (the search no longer coin-flips). The verdict may still be `divergent` (the separate fix-precision issue is out of scope) — what this verifies is that grounding deterministically consults docs.

## Self-Review (completed by author)

- **Spec coverage:** force via `tool_choice={"type":"tool","name":"web_search"}` when `web_search` — Step 3 + force assertion (Step 1a); no forcing when off — Step 1b assertion; one-retry unforced fallback — Step 3 try/except + fallback test (Step 1c); non-web_search errors propagate — Step 3 `if "tool_choice" not in kwargs: raise`; single-file scope — only claude.py + its test; live metric — Final verification.
- **Placeholder scan:** none — every step has complete code.
- **Type consistency:** `complete(..., web_search=False)` signature unchanged; `tool_choice` literal identical in code and the force assertion; the fallback test asserts the exact two-call sequence the implementation produces.
