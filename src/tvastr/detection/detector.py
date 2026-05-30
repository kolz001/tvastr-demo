"""Failure detector — clusters events and classifies each pattern's sensitivity.

Sensitivity is determined locally (regex PII scan) so the decision itself never
requires an external call. Patterns whose underlying events contain PII are marked
SENSITIVE, which keeps their raw content on the local boundary during routing.
"""

from __future__ import annotations

from tvastr.detection.clustering import cluster_events, fingerprint
from tvastr.domain import FailurePattern, LogEvent, Sensitivity
from tvastr.logging import get_logger
from tvastr.pii import contains_pii

log = get_logger(__name__)


def _event_text(event: LogEvent) -> str:
    parts = [event.message, event.stack_trace or "", *event.attributes.values()]
    return "\n".join(parts)


class FailureDetector:
    def detect(self, events: list[LogEvent]) -> list[FailurePattern]:
        patterns = cluster_events(events)

        events_by_fp: dict[str, list[LogEvent]] = {}
        for event in events:
            events_by_fp.setdefault(fingerprint(event), []).append(event)

        for pattern in patterns:
            cluster_events_ = events_by_fp.get(pattern.fingerprint, [])
            if any(contains_pii(_event_text(e)) for e in cluster_events_):
                pattern.sensitivity = Sensitivity.SENSITIVE

        log.info(
            "detect.complete",
            events=len(events),
            patterns=len(patterns),
            sensitive=sum(1 for p in patterns if p.sensitivity is Sensitivity.SENSITIVE),
        )
        return patterns
