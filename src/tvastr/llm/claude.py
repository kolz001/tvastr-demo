"""Cloud LLM backend (Claude).

Handles complex reasoning where accuracy and language quality matter most:
root-cause analysis, code fix generation, and PR descriptions. Only ever receives
data that the router has cleared as non-sensitive (and PII-redacted).
"""

from __future__ import annotations

from tvastr.llm.base import LLMResponse
from tvastr.logging import get_logger

log = get_logger(__name__)

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


class ClaudeClient:
    target = "cloud"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(
        self, prompt: str, *, system: str | None = None, web_search: bool = False
    ) -> LLMResponse:
        import anthropic  # lazy import: only needed when not mocking

        client = anthropic.Anthropic(api_key=self.api_key)
        log.info("llm.cloud.complete", model=self.model, web_search=web_search)
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system or "You are a senior software engineer fixing production bugs.",
            "messages": [{"role": "user", "content": prompt}],
        }
        if web_search:
            kwargs["tools"] = [_WEB_SEARCH_TOOL]
            kwargs["tool_choice"] = {"type": "tool", "name": "web_search"}
        try:
            message = client.messages.create(**kwargs)
        except anthropic.BadRequestError:
            # Some API configs reject forcing the server-side web_search tool with
            # a 400; retry once offered (non-forced) so grounding still runs. Other
            # errors (transient network, auth) propagate unchanged.
            if "tool_choice" not in kwargs:
                raise
            log.warning("llm.cloud.web_search.force_rejected", model=self.model)
            kwargs.pop("tool_choice")
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


class MockClaudeClient:
    """Deterministic stand-in for Claude — no API key required.

    Returns structured JSON when the prompt asks for a fix proposal (so the
    fix-generation tool's search/replace path is exercised end-to-end), and a
    prose explanation otherwise.
    """

    target = "cloud"

    def __init__(self, model: str = "claude-opus-4-7") -> None:
        self.model = model

    def complete(
        self, prompt: str, *, system: str | None = None, web_search: bool = False
    ) -> LLMResponse:
        log.info("llm.cloud.complete", model=self.model, mocked=True)
        if self._looks_like_fix_prompt(prompt, system):
            text = self._mock_fix_json(prompt)
        elif self._looks_like_investigate_prompt(system):
            text = self._mock_investigate_json(prompt)
        else:
            text = (
                "[cloud-reasoning] Based on the failure signature, the most likely root "
                "cause is a contract mismatch between connected components. Recommended fix: "
                "align the producing component's output type with the consumer's expected "
                "input, and add a regression test that exercises the connection."
            )
        return LLMResponse(text=text, model=self.model, target=self.target, mocked=True)

    @staticmethod
    def _looks_like_fix_prompt(prompt: str, system: str | None) -> bool:
        haystack = f"{system or ''}\n{prompt}"
        return '"search"' in haystack and '"replace"' in haystack

    @staticmethod
    def _looks_like_investigate_prompt(system: str | None) -> bool:
        # The investigator system prompt contains this unique sentinel phrase.
        return '"done": true' in (system or "")

    @staticmethod
    def _mock_investigate_json(prompt: str) -> str:
        """Return a plausible root-cause finish for the investigator loop."""
        import re

        # Extract a suspected file path from the prompt if any are mentioned.
        match = re.search(r"# ── (\S+) ──", prompt)
        suspected = f'["{match.group(1)}"]' if match else "[]"
        return (
            '{"root_cause": "[mock] contract mismatch detected in the suspected file; '
            "the producer's output type does not match the consumer's expected input type. "
            'Align the types and add a regression test.", '
            f'"suspected_files": {suspected}, "confidence": 0.9, "done": true}}'
        )

    @staticmethod
    def _mock_fix_json(prompt: str) -> str:
        """Find a real substring of the retrieved source and propose a comment-insertion.

        Locates the first ``# ── <path> ──`` section and the first non-empty,
        non-header line in it, then proposes inserting a deterministic marker
        comment above that line. The result is a valid, applicable fix — enough
        to exercise the parser, validator, and ``str.replace`` path end-to-end.
        """
        import re

        section = re.search(r"# ── (\S+) ──\n(.+?)(?=\n# ── |\Z)", prompt, re.DOTALL)
        if not section:
            return '{"summary": "no source retrieved", "changes": [], "test_plan": "n/a"}'
        path = section.group(1)
        body = section.group(2)
        # Pick the first non-blank line as the anchor.
        anchor = next((ln for ln in body.splitlines() if ln.strip()), None)
        if anchor is None:
            return '{"summary": "empty file", "changes": [], "test_plan": "n/a"}'
        marker = "# tvastr-mock: proposed fix anchor"
        replace = f"{marker}\n{anchor}"
        return (
            '{"summary": "[mock-fix] insert a marker comment above the first '
            'statement of the suspected file to exercise the search/replace path.", '
            '"changes": [{"path": ' + _json_str(path) + ', "search": ' + _json_str(anchor)
            + ", \"replace\": " + _json_str(replace)
            + ', "rationale": "deterministic mock edit"}], '
            '"test_plan": "Assert the marker appears in the patched file."}'
        )


def _json_str(s: str) -> str:
    """Minimal JSON-safe string encoder for embedding into a hand-built JSON payload."""
    import json as _json

    return _json.dumps(s)
