"""LLM access layer: a uniform client interface, concrete local/cloud backends,
and the hybrid router that chooses between them based on data sensitivity.
"""

from tvastr.llm.base import LLMClient, LLMResponse
from tvastr.llm.claude import ClaudeClient, MockClaudeClient
from tvastr.llm.local import MockOllamaClient, OllamaClient
from tvastr.llm.router import HybridRouter, build_router

__all__ = [
    "ClaudeClient",
    "HybridRouter",
    "LLMClient",
    "LLMResponse",
    "MockClaudeClient",
    "MockOllamaClient",
    "OllamaClient",
    "build_router",
]
