from tvastr.detection import FailureDetector, ThresholdEngine, cluster_events, fingerprint
from tvastr.domain import LogEvent, Sensitivity


def test_same_failure_different_ids_clusters_together():
    a = LogEvent(service="svc", message="KeyError: 'doc-4f9a2' not found in index")
    b = LogEvent(service="svc", message="KeyError: 'doc-7b1c8' not found in index")
    assert fingerprint(a) == fingerprint(b)
    clusters = cluster_events([a, b])
    assert len(clusters) == 1
    assert clusters[0].count == 2


def test_clusters_sorted_by_frequency(recurring_events):
    clusters = cluster_events(recurring_events)
    assert clusters[0].count == 3  # the recurring import failure leads
    assert clusters[0].is_recurring


def test_detector_flags_pii_as_sensitive(recurring_events, sensitive_event):
    patterns = FailureDetector().detect([*recurring_events, sensitive_event])
    sensitive = [p for p in patterns if p.sensitivity is Sensitivity.SENSITIVE]
    assert len(sensitive) == 1
    assert "ConnectionError" in sensitive[0].title


def test_threshold_selects_only_recurring(recurring_events):
    patterns = FailureDetector().detect(recurring_events)
    selected = ThresholdEngine(recurrence_threshold=3).select(patterns)
    assert len(selected) == 1
    assert selected[0].count == 3


def test_threshold_dedups_handled_patterns(recurring_events):
    patterns = FailureDetector().detect(recurring_events)
    engine = ThresholdEngine(recurrence_threshold=3)
    first = engine.select(patterns)
    engine.mark_handled(first[0])
    assert engine.select(patterns) == []
