"""Regex-based PII / secret redaction.

A deliberately conservative first pass for the foundation: emails, API keys and
bearer tokens, IPs, and credentialed URLs. The weeks 3-4 milestone replaces/augments
this with a local-model classifier, but the interface (:func:`redact`,
:func:`contains_pii`) stays stable so callers don't change.
"""

from __future__ import annotations

import re

# (label, pattern) — ordered most-specific first. CREDENTIAL_URL precedes EMAIL so a
# credentialed URL is captured whole, before EMAIL claims its userinfo.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9\-_]{8,}\b")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", re.IGNORECASE)),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("CREDENTIAL_URL", re.compile(r"\b(?:https?|opensearch)://[^\s/@]+@[^\s]+", re.IGNORECASE)),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, labels_found)``.

    Each match is replaced by ``[REDACTED:LABEL]``. ``labels_found`` lists the
    kinds of PII detected (deduplicated, in detection order).
    """
    found: list[str] = []
    redacted = text
    for label, pattern in _PATTERNS:
        if pattern.search(redacted):
            if label not in found:
                found.append(label)
            redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted, found


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for _, pattern in _PATTERNS)
