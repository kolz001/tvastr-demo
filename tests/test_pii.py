import pytest

from tvastr.pii import _presidio, contains_pii, redact, redaction
from tvastr.pii._presidio import Span


def test_redacts_email_and_api_key():
    text = "user jane.doe@example.com token sk-ant-REDACTEDABC123"
    redacted, found = redact(text)
    assert "jane.doe@example.com" not in redacted
    assert "sk-ant-REDACTEDABC123" not in redacted
    assert "EMAIL" in found
    assert "API_KEY" in found


def test_redacts_credentialed_url_and_ip():
    text = "connect to https://admin@search.internal:9200 from 10.0.1.42"
    redacted, found = redact(text)
    assert "admin@search.internal" not in redacted
    assert "CREDENTIAL_URL" in found
    assert "IP" in found


def test_clean_text_is_untouched():
    text = "PipelineConnectError: type mismatch between components"
    redacted, found = redact(text)
    assert redacted == text
    assert found == []
    assert contains_pii(text) is False


# --- Hybrid layer: regex floor + optional local NER -------------------------
# These stub the model seam so they run without the (optional, heavy) pii extra.


def test_model_layer_redacts_person_alongside_regex(monkeypatch):
    """When the local model is active it adds spans (e.g. a name) that the regex
    floor can't express, unioned with the regex hits in the same text."""
    text = "Contact Jane Doe at jane@example.com"

    def fake_model_spans(t):
        i = t.index("Jane Doe")
        return [Span(i, i + len("Jane Doe"), "PERSON")]

    monkeypatch.setattr(redaction, "model_spans", fake_model_spans)
    out, found = redact(text)
    assert "Jane Doe" not in out
    assert "jane@example.com" not in out
    assert "[REDACTED:PERSON]" in out
    assert "PERSON" in found and "EMAIL" in found


def test_regex_wins_over_overlapping_model_span(monkeypatch):
    """If the model claims a span the regex also covers, the deterministic regex
    label wins — no double-redaction, no model label leaks into the result."""
    text = "mail jane.doe@example.com"

    def fake_model_spans(t):
        i = t.index("jane.doe")  # model mis-tags the local-part as a PERSON
        return [Span(i, i + len("jane.doe"), "PERSON")]

    monkeypatch.setattr(redaction, "model_spans", fake_model_spans)
    out, found = redact(text)
    assert out == "mail [REDACTED:EMAIL]"
    assert found == ["EMAIL"]
    assert "PERSON" not in found


def test_model_not_consulted_when_disabled(monkeypatch):
    """Default flag is off: model_spans short-circuits before touching the
    analyzer, so behavior (and cost) is identical to regex-only."""
    calls = {"n": 0}

    def tracking_analyzer():
        calls["n"] += 1
        return None

    monkeypatch.setattr(_presidio, "_analyzer", tracking_analyzer)
    assert _presidio.model_spans("Jane Doe lives in Berlin") == []
    assert calls["n"] == 0  # never built the engine — disabled short-circuit


def test_model_fail_open_when_engine_unavailable(monkeypatch):
    """Flag on but the engine can't be built (extra/model missing) → no spans,
    no exception: redaction falls back to the regex floor."""
    monkeypatch.setattr(_presidio, "_enabled", lambda: True)
    monkeypatch.setattr(_presidio, "_analyzer", lambda: None)
    assert _presidio.model_spans("Jane Doe") == []


def test_model_fail_open_when_analyze_raises(monkeypatch):
    """A runtime failure inside the analyzer must not propagate."""

    class BoomEngine:
        def analyze(self, **kwargs):
            raise RuntimeError("nlp engine blew up")

    monkeypatch.setattr(_presidio, "_enabled", lambda: True)
    monkeypatch.setattr(_presidio, "_analyzer", lambda: BoomEngine())
    assert _presidio.model_spans("Jane Doe") == []


def test_model_maps_known_entities_and_drops_unmapped(monkeypatch):
    """Presidio entity types map to our labels; types we don't map (e.g.
    DATE_TIME) are dropped rather than redacted as noise."""

    class _Result:
        def __init__(self, entity_type, start, end):
            self.entity_type = entity_type
            self.start = start
            self.end = end

    class Engine:
        def analyze(self, text, language, entities, score_threshold):
            return [
                _Result("PERSON", 0, 4),
                _Result("DATE_TIME", 5, 9),
                _Result("US_SSN", 10, 21),
            ]

    monkeypatch.setattr(_presidio, "_enabled", lambda: True)
    monkeypatch.setattr(_presidio, "_analyzer", lambda: Engine())
    labels = {s.label for s in _presidio.model_spans("Jane 2024 123-45-6789")}
    assert labels == {"PERSON", "SSN"}  # DATE_TIME is unmapped → dropped


def test_presidio_integration_detects_person(monkeypatch):
    """Real end-to-end check, skipped unless the pii extra + spaCy model are
    actually installed (engine builds)."""
    pytest.importorskip("presidio_analyzer")
    _presidio._analyzer.cache_clear()
    engine = _presidio._analyzer()
    if engine is None:
        pytest.skip("presidio installed but spaCy model unavailable")
    monkeypatch.setattr(_presidio, "_enabled", lambda: True)
    out, found = redact("Please escalate this to Margaret Hamilton immediately.")
    assert "Margaret Hamilton" not in out
    assert "PERSON" in found
