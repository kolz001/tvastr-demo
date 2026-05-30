"""Cloud LLM backend (Claude).

Handles complex reasoning where accuracy and language quality matter most:
root-cause analysis, code fix generation, and PR descriptions. Only ever receives
data that the router has cleared as non-sensitive (and PII-redacted).
"""

from __future__ import annotations

from tvastr.llm.base import LLMResponse
from tvastr.logging import get_logger

log = get_logger(__name__)


class ClaudeClient:
    target = "cloud"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        import anthropic  # lazy import: only needed when not mocking

        client = anthropic.Anthropic(api_key=self.api_key)
        log.info("llm.cloud.complete", model=self.model)
        message = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system or "You are a senior software engineer fixing production bugs.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(text=text, model=self.model, target=self.target)


class MockClaudeClient:
    """Deterministic stand-in for Claude — no API key required.

    Produces structured-looking output so downstream parsing and the demo work
    end-to-end offline.
    """

    target = "cloud"

    def __init__(self, model: str = "claude-opus-4-7") -> None:
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        log.info("llm.cloud.complete", model=self.model, mocked=True)
        text = (
            "[cloud-reasoning] Based on the failure signature, the most likely root "
            "cause is a contract mismatch between connected components. Recommended fix: "
            "align the producing component's output type with the consumer's expected "
            "input, and add a regression test that exercises the connection."
        )
        return LLMResponse(text=text, model=self.model, target=self.target, mocked=True)
