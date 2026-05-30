"""Centralized, environment-driven configuration.

Every setting is overridable via a ``TVASTR_*`` environment variable (or ``.env``).
``use_mocks`` is the master switch for local-first development: when true (default),
no external service or secret is required and every integration is backed by an
in-memory mock.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TVASTR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ---
    env: Literal["local", "staging", "production"] = "local"
    use_mocks: bool = True
    log_level: str = "INFO"
    log_json: bool = False

    # --- Local LLM (Ollama) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    # --- Cloud LLM (Claude) ---
    # Read from the conventional ANTHROPIC_API_KEY (no TVASTR_ prefix).
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_model: str = "claude-opus-4-7"

    # --- Storage (OpenSearch) ---
    opensearch_url: str = "http://localhost:9200"
    opensearch_user: str = "admin"
    opensearch_password: str | None = None
    opensearch_log_index: str = "tvastr-logs"
    opensearch_audit_index: str = "tvastr-audit"

    # --- GitHub ---
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_repo: str = "deepset-ai/haystack"
    github_base_branch: str = "main"

    # --- Slack ---
    slack_webhook_url: str | None = None

    # --- Detection thresholds ---
    recurrence_threshold: int = 3
    dedup_window_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
