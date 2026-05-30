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
            service="haystack-pipeline",
            severity=Severity.ERROR,
            message="ValueError: Missing required input variable 'question'",
            stack_trace='File "haystack/components/builders/prompt_builder.py", line 142',
        )
        for i in range(3)
    ]
    events.append(
        LogEvent(
            timestamp=base,
            service="haystack-converter",
            severity=Severity.ERROR,
            message="PyPDFError: Could not read malformed PDF",
        )
    )
    return events


@pytest.fixture
def sensitive_event() -> LogEvent:
    return LogEvent(
        service="haystack-retriever",
        message=(
            "ConnectionError: failed for user jane.doe@example.com token sk-ant-REDACTEDABC123"
        ),
    )
