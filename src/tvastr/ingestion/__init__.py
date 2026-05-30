"""Ingestion layer — pulls raw log events from a source into the pipeline.

In production this is fed by CloudWatch -> EventBridge -> SQS -> Lambda. For
local-first development the :class:`SimulatedLogSource` replays sample failure
logs from disk.
"""

from tvastr.ingestion.base import LogSource
from tvastr.ingestion.cloudwatch import CloudWatchLogSource
from tvastr.ingestion.simulated import SimulatedLogSource, default_sample_path

__all__ = [
    "CloudWatchLogSource",
    "LogSource",
    "SimulatedLogSource",
    "default_sample_path",
]
