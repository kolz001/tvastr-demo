"""Tests for the Anthropic web_search wiring."""

from __future__ import annotations

from tvastr.llm.base import LLMResponse
from tvastr.llm.claude import ClaudeClient
from tvastr.llm.router import HybridRouter, TaskType


def test_llmresponse_sources_defaults_empty():
    r = LLMResponse(text="x", model="m", target="cloud")
    assert r.sources == []


def test_doc_grounding_is_a_cloud_task():
    # Not in the local set, so it routes to cloud (and gets redacted).
    from tvastr.llm.router import _LOCAL_TASKS
    assert TaskType.DOC_GROUNDING.value == "doc_grounding"
    assert TaskType.DOC_GROUNDING not in _LOCAL_TASKS


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
