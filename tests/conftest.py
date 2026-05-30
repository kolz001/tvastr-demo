from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tvastr.config import Settings
from tvastr.domain import LogEvent, Severity


@pytest.fixture
def settings() -> Settings:
    return Settings(use_mocks=True, recurrence_threshold=3, dedup_window_minutes=60)


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
