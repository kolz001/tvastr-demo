"""Optional local NER layer for PII detection (Microsoft Presidio + spaCy).

This augments the deterministic regex redactor with a *local* model that catches
unstructured PII (names, locations, organisations, phone numbers, …) which regex
cannot express. It is:

- **Optional** — requires the ``pii`` extra (``pip install tvastr[pii]``) plus a
  spaCy model (``python -m spacy download en_core_web_lg``). Absent either, this
  module is inert and redaction falls back to the regex floor.
- **Local** — Presidio/spaCy run entirely on-device. Nothing here makes a
  network call; the whole point is to detect PII *before* the cloud boundary,
  never by leaking it to detect it.
- **Additive & fail-open** — it can only *add* spans. Any failure (missing dep,
  model load error, analysis error) logs once and yields no spans, so we never
  redact *less* than the regex floor guarantees.

The analyzer is built lazily and once; the engine load (spaCy model) is the
expensive part, so it is cached for the process lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from tvastr.config import get_settings
from tvastr.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Span:
    """A detected PII region as offsets into the original text."""

    start: int
    end: int
    label: str


# Presidio entity type -> our redaction label. Entities the regex floor already
# owns deterministically (EMAIL_ADDRESS, IP_ADDRESS, URL) are intentionally
# excluded so the regex stays the single source of truth for those.
_ENTITY_LABELS: dict[str, str] = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "NRP": "ORG",
    "ORGANIZATION": "ORG",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CREDIT_CARD",
    "US_SSN": "SSN",
    "IBAN_CODE": "IBAN",
}

# spaCy NER is probabilistic; drop weak hits so we don't redact common nouns.
_SCORE_THRESHOLD = 0.5


@lru_cache(maxsize=1)
def _analyzer() -> object | None:
    """Build the Presidio AnalyzerEngine once, or None if unavailable.

    Cached for the process lifetime — the spaCy model load is the costly step.
    Returns None (never raises) when the ``pii`` extra isn't installed or the
    engine can't be built, so callers degrade to regex-only.
    """
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError:
        log.info(
            "pii.local_model.unavailable",
            reason="presidio not installed — `pip install tvastr[pii]`",
        )
        return None
    try:
        return AnalyzerEngine()
    except Exception as exc:  # model missing, NLP engine init failure, etc.
        log.warning("pii.local_model.load_failed", error=str(exc))
        return None


def _enabled() -> bool:
    return bool(get_settings().pii_local_model)


def model_spans(text: str) -> list[Span]:
    """Return local-NER PII spans for ``text``; ``[]`` when disabled/unavailable.

    Fail-open by contract: returns ``[]`` on any error so the regex floor still
    applies. Only emits spans for the entity types in ``_ENTITY_LABELS``.
    """
    if not _enabled():
        return []
    engine = _analyzer()
    if engine is None:
        return []
    try:
        results = engine.analyze(  # type: ignore[attr-defined]
            text=text,
            language="en",
            entities=list(_ENTITY_LABELS),
            score_threshold=_SCORE_THRESHOLD,
        )
    except Exception as exc:
        log.warning("pii.local_model.analyze_failed", error=str(exc))
        return []
    spans: list[Span] = []
    for r in results:
        label = _ENTITY_LABELS.get(getattr(r, "entity_type", ""))
        if label:
            spans.append(Span(start=r.start, end=r.end, label=label))
    return spans
