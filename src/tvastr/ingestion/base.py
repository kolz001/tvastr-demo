"""The LogSource protocol that every ingestion backend implements."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from tvastr.domain import LogEvent


@runtime_checkable
class LogSource(Protocol):
    """A pullable stream of log events.

    Implementations must be safe to iterate once; callers that need to re-read
    should materialize the iterator (e.g. ``list(source.read())``).
    """

    name: str

    def read(self) -> Iterable[LogEvent]:
        """Yield log events from the underlying source."""
        ...


def collect(source: LogSource, limit: int | None = None) -> list[LogEvent]:
    """Drain a source into a list, optionally capped at ``limit`` events."""
    events: list[LogEvent] = []
    it: Iterator[LogEvent] = iter(source.read())
    for event in it:
        events.append(event)
        if limit is not None and len(events) >= limit:
            break
    return events
