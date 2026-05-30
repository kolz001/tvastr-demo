"""Pattern detection layer — turns a stream of log events into recurring,
actionable :class:`~tvastr.domain.FailurePattern`s.

Pipeline: cluster events by fingerprint -> summarize each cluster with the local
model (and classify sensitivity) -> apply the threshold engine to keep only
patterns worth remediating.
"""

from tvastr.detection.clustering import cluster_events, fingerprint
from tvastr.detection.detector import FailureDetector
from tvastr.detection.threshold import ThresholdEngine

__all__ = ["FailureDetector", "ThresholdEngine", "cluster_events", "fingerprint"]
