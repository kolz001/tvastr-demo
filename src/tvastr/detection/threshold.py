"""Threshold engine — decides which patterns are worth remediating.

A rule-based gate (no LLM): a pattern qualifies once it recurs at least
``recurrence_threshold`` times. Deduplication suppresses patterns already handled
within ``dedup_window_minutes`` so the agent doesn't open duplicate PRs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tvastr.domain import FailurePattern
from tvastr.logging import get_logger

log = get_logger(__name__)


class ThresholdEngine:
    def __init__(self, recurrence_threshold: int = 3, dedup_window_minutes: int = 60) -> None:
        self.recurrence_threshold = recurrence_threshold
        self.dedup_window = timedelta(minutes=dedup_window_minutes)
        # fingerprint -> last time we acted on it
        self._handled: dict[str, datetime] = {}

    def _is_duplicate(self, pattern: FailurePattern, now: datetime) -> bool:
        last = self._handled.get(pattern.fingerprint)
        return last is not None and (now - last) < self.dedup_window

    def select(
        self, patterns: list[FailurePattern], *, now: datetime | None = None
    ) -> list[FailurePattern]:
        """Return patterns that meet the recurrence threshold and aren't duplicates."""
        now = now or datetime.now(UTC)
        selected: list[FailurePattern] = []
        for pattern in patterns:
            if pattern.count < self.recurrence_threshold:
                continue
            if self._is_duplicate(pattern, now):
                log.info("threshold.dedup_skip", fingerprint=pattern.fingerprint)
                continue
            selected.append(pattern)
        log.info(
            "threshold.select",
            candidates=len(patterns),
            selected=len(selected),
            recurrence_threshold=self.recurrence_threshold,
        )
        return selected

    def mark_handled(self, pattern: FailurePattern, *, now: datetime | None = None) -> None:
        self._handled[pattern.fingerprint] = now or datetime.now(UTC)
