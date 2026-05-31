"""Stream LogEvents in over stdin.

Lets anyone with logs in any system pipe JSONL into tvastr without writing an
adapter:

    cat my.log | jq -c '{service, message, stack_trace}' | tvastr demo --logs -

Each line on stdin is parsed as a ``LogEvent`` using the shared JSONL helper,
so blank lines and ``#`` comments are skipped and malformed lines are logged
and skipped rather than aborting the stream.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from tvastr.domain import LogEvent
from tvastr.ingestion.base import parse_jsonl_events
from tvastr.logging import get_logger

log = get_logger(__name__)


class StdinLogSource:
    name = "stdin"

    def read(self) -> Iterable[LogEvent]:
        log.info("ingest.read", source=self.name)
        yield from parse_jsonl_events(sys.stdin, source_name=self.name)
