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
    # When true, the agent goes through the whole flow (real or mock backends) but
    # the final PR-creation side effect is suppressed. Use to inspect what the
    # agent *would* do against a real repo before letting it actually push.
    dry_run: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    # --- PII redaction ---
    # When true, the regex redactor is augmented with a local Presidio (spaCy
    # NER) layer that catches unstructured PII (names, locations, orgs). Off by
    # default: requires the ``pii`` extra + a spaCy model; absent either, or on
    # any model failure, redaction falls back to the regex floor (fail-open).
    pii_local_model: bool = False

    # --- Documentation grounding ---
    # When true (live mode + Anthropic key), the agent runs a web-search-grounded
    # diagnosis check before generating a fix. Off by default; no-op in mock mode.
    doc_grounding: bool = False

    # When true, the agent reads repository code as of the issue's creation date
    # (so moved/deleted paths resolve and still contain the bug) by wrapping the
    # code host with IssueEraCodeHost. Off in tests.
    issue_era_retrieval: bool = True

    # When true, doc-grounding may install the diagnosis-relevant third-party
    # SDK (host pip, wheels-only, no-deps, into data/sdk_cache/) and inject its
    # type definitions into the grounding prompt as ground truth for field
    # names. Off in tests.
    sdk_schema_grounding: bool = True

    # --- Triage UI ---
    # When true, the triage UI auto-analyzes the top 5 discovered PRs on load
    # (one cloud LLM call each). Off by default so loading the issue list never
    # silently spends cloud calls; users can still analyze any PR on demand via
    # the per-card "Analyze PR" button.
    auto_analyze_prs: bool = False

    # --- Local LLM (Ollama) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    # --- Cloud LLM (Claude) ---
    # Read from the conventional ANTHROPIC_API_KEY (no TVASTR_ prefix).
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_model: str = "claude-opus-4-7"

    # --- Audit storage ---
    # "memory"  — in-process list; loses data on exit. Best for tests/ephemeral runs.
    # "file"    — append-only JSONL on disk; zero infrastructure. Default.
    # "opensearch" — production path; requires a reachable OpenSearch cluster.
    audit_backend: Literal["memory", "file", "opensearch"] = "file"
    audit_file_path: str = "data/audit/tvastr-audit.jsonl"

    # --- Storage (OpenSearch) — used when audit_backend="opensearch" ---
    opensearch_url: str = "http://localhost:9200"
    opensearch_user: str = "admin"
    opensearch_password: str | None = None
    opensearch_log_index: str = "tvastr-logs"
    opensearch_audit_index: str = "tvastr-audit"

    # --- Grafana Loki (optional log source; OSS, Apache-2.0, self-hostable) ---
    loki_url: str | None = None              # e.g. http://localhost:3100
    loki_query: str | None = None            # default LogQL query, e.g. '{app="myapp"} |= "Error"'
    loki_user: str | None = Field(default=None, alias="LOKI_USER")
    loki_password: str | None = Field(default=None, alias="LOKI_PASSWORD")

    # --- GitHub ---
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_repo: str = "run-llama/llama_index"
    github_base_branch: str = "main"

    # --- Slack ---
    slack_webhook_url: str | None = None

    # --- Detection thresholds ---
    recurrence_threshold: int = 3
    dedup_window_minutes: int = 60

    # --- Verification ---
    # Sandbox for the verify-fix loop. "auto" picks docker if available, else subprocess.
    verify_sandbox: Literal["auto", "docker", "subprocess"] = "auto"
    verify_docker_image: str = "tvastr-verify:llamaindex"
    # Project root scanned for scoped regression tests. Empty disables that oracle.
    verify_project_root: str = ""
    # When true, the verify sandbox installs the issue's integration package(s)
    # on demand (pip install --target) before running the reproducer, so the
    # long tail of llama_index integrations is importable. Needs network for the
    # prep step; baseline/rerun stay network-isolated. Off in tests.
    verify_provision_deps: bool = True
    # When true, and the released wheel already contains a closed issue's merged
    # fix (baseline returns no_repro), verify overlays the PRE-FIX version of the
    # fixing PR's changed files (fetched at the buggy commit) so the baseline
    # reproduces and the rerun yields a real verdict. Off in tests.
    verify_source_overlay: bool = True
    # When true, a verify cycle that comes back REPRO_BROKEN (the reproducer broke
    # in its own scaffolding, e.g. a hand-rolled fake of an SDK object) is retried
    # with a reproducer repaired against the real installed deps. Off in tests.
    verify_repro_repair: bool = True


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
