"""Local LLM backend (Ollama).

Handles sensitive work that must not leave the local/VPC boundary: log parsing,
failure summarization, and PII redaction. The mock variant lets the whole pipeline
run offline with no Ollama daemon.
"""

from __future__ import annotations

from tvastr.llm.base import LLMResponse
from tvastr.logging import get_logger

log = get_logger(__name__)


class OllamaClient:
    target = "local"

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        import ollama  # lazy import: only needed when not mocking

        client = ollama.Client(host=self.base_url)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        log.info("llm.local.complete", model=self.model)
        result = client.chat(model=self.model, messages=messages)
        return LLMResponse(
            text=result["message"]["content"],
            model=self.model,
            target=self.target,
        )


class MockOllamaClient:
    """Deterministic stand-in for a local model — no daemon required."""

    target = "local"

    def __init__(self, model: str = "llama3.1") -> None:
        self.model = model

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        log.info("llm.local.complete", model=self.model, mocked=True)
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        text = (
            f"[local-summary] Recurring failure detected. Representative signal: {first_line[:160]}"
        )
        return LLMResponse(text=text, model=self.model, target=self.target, mocked=True)
