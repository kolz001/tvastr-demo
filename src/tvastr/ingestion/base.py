"""The LogSource protocol that every ingestion backend implements."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from tvastr.domain import LogEvent
from tvastr.logging import get_logger

log = get_logger(__name__)


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


def parse_jsonl_events(lines: Iterable[str], *, source_name: str) -> Iterable[LogEvent]:
    """Parse an iterable of JSONL lines into ``LogEvent``s.

    Skips blank lines and ``#`` comments; logs and skips lines that fail to
    parse rather than aborting the whole stream — a single garbled line in a
    file or stdin shouldn't kill the pipeline.
    """
    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            yield LogEvent.model_validate(json.loads(stripped))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "ingest.skip_malformed", source=source_name, line=line_no, error=str(exc)
            )
