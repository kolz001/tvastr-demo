"""Common interface shared by local and cloud LLM backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMResponse(BaseModel):
    text: str
    model: str
    target: str  # "local" | "cloud"
    mocked: bool = False
    sources: list[str] = Field(default_factory=list)  # citation URLs (web_search)


@runtime_checkable
class LLMClient(Protocol):
    """A minimal text-completion client.

    ``target`` distinguishes local (Ollama, in-VPC) from cloud (Claude) backends
    so the router and audit log can reason about where data flowed.
    """

    model: str
    target: str

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        """Return a completion for ``prompt`` with an optional ``system`` preamble."""
        ...
