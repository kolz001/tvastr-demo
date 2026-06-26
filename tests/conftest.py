from __future__ import annotations

import os

# Force-hermetic test settings BEFORE importing any tvastr code. ``get_settings``
# is ``lru_cache``'d, so if the test runner has ``.env`` lying around with
# ``TVASTR_USE_MOCKS=false`` and live keys, every API test would otherwise hit
# real Claude/GitHub. Pin to mocks + in-memory audit at the env layer so the
# whole test process is sealed off from local .env state.
os.environ["TVASTR_USE_MOCKS"] = "true"
os.environ["TVASTR_AUDIT_BACKEND"] = "memory"
os.environ["TVASTR_RECURRENCE_THRESHOLD"] = "3"
# Seal off the optional local-NER layer too: a developer's .env may enable it,
# which would make redaction non-deterministic across machines (and require the
# pii extra installed). Tests that exercise it opt in by monkeypatching.
os.environ["TVASTR_PII_LOCAL_MODEL"] = "false"
# Seal the doc-grounding flag too — a developer's .env may enable it, which would
# make grounding (a live web-search call) fire and flip default-state tests.
os.environ["TVASTR_DOC_GROUNDING"] = "false"
# Pin dry_run=false so tests start from the autonomous-mode baseline. Tests
# that need dry-run behaviour pass ``Settings(..., dry_run=True)`` explicitly.
os.environ["TVASTR_DRY_RUN"] = "false"
# Seal the verify-provisioning flag — a developer's .env may enable it, which
# would make the verify sandbox pip-install over the network during tests.
os.environ["TVASTR_VERIFY_PROVISION_DEPS"] = "false"
# Clear any token-shaped values from ``.env`` so live-path code refuses to fire.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GITHUB_TOKEN"] = ""

from datetime import UTC, datetime, timedelta

import pytest

from tvastr.config import Settings, get_settings
from tvastr.domain import LogEvent, Severity

# Make sure no stale cached Settings from a previous import linger.
get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    # audit_backend=memory keeps tests hermetic (no files written).
    return Settings(
        use_mocks=True,
        audit_backend="memory",
        recurrence_threshold=3,
        dedup_window_minutes=60,
    )


@pytest.fixture
def recurring_events() -> list[LogEvent]:
    """Three occurrences of the same failure plus one unrelated singleton."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    events = [
        LogEvent(
            timestamp=base + timedelta(minutes=i),
            service="llamaindex-app",
            severity=Severity.ERROR,
            message="ModuleNotFoundError: No module named 'llama_index.llms.openai'",
            stack_trace=(
                'File "app/rag.py", line 12, in <module>\n'
                "    from llama_index.llms.openai import OpenAI"
            ),
        )
        for i in range(3)
    ]
    events.append(
        LogEvent(
            timestamp=base,
            service="llamaindex-agent",
            severity=Severity.ERROR,
            message="RuntimeError: This event loop is already running",
        )
    )
    return events


@pytest.fixture
def sensitive_event() -> LogEvent:
    return LogEvent(
        service="llamaindex-vector-store",
        message=(
            "ConnectionError: failed for user jane.doe@example.com token sk-ant-REDACTEDABC123"
        ),
    )


@pytest.fixture(autouse=True)
def clear_pr_discovery_cache() -> None:
    """Clear the pr_discovery cache before each test for test isolation."""
    from tvastr.analysis import pr_discovery

    pr_discovery._cache.clear()
