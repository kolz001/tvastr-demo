"""CloudWatch Logs source (production ingestion path).

Stubbed for the local-first foundation. The production wiring is
CloudWatch Logs -> EventBridge -> SQS -> Lambda, where the Lambda hands batches
of events to this source. Implemented in the AWS deployment phase (weeks 5-6).
"""

from __future__ import annotations

from collections.abc import Iterable

from tvastr.domain import LogEvent


class CloudWatchLogSource:
    name = "cloudwatch"

    def __init__(self, log_group: str, region: str = "us-east-1") -> None:
        self.log_group = log_group
        self.region = region

    def read(self) -> Iterable[LogEvent]:
        raise NotImplementedError(
            "CloudWatch ingestion is implemented in the AWS deployment phase. "
            "Use SimulatedLogSource for local development."
        )
