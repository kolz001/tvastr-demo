"""Hybrid PII / secret redaction: a deterministic regex floor plus an optional
local NER layer (Microsoft Presidio + spaCy) for the unstructured long tail.

The regex layer always runs and is the source of truth for structured secrets:
API keys, bearer/AWS tokens, credentialed URLs, emails, IPs — each replaced by
``[REDACTED:LABEL]``. When the local model is enabled (``pii_local_model`` + the
``pii`` extra) it *adds* spans for names, locations, organisations, phone
numbers, etc. The regex always wins on overlap, and any model failure degrades
to regex only — so we never redact *less* than the floor guarantees.

The public interface (:func:`redact`, :func:`contains_pii`) is stable; callers
don't change regardless of whether the model layer is active.
"""

from __future__ import annotations

import re

from tvastr.pii._presidio import Span, model_spans

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

# Internal candidate span: (priority, start, end, label). Lower priority wins on
# overlap. Regex patterns take priority = their index in _PATTERNS (so earlier,
# more-specific patterns win); model spans sit below all regex patterns.
_Candidate = tuple[int, int, int, str]
_MODEL_PRIORITY = len(_PATTERNS)


def _regex_candidates(text: str) -> list[_Candidate]:
    out: list[_Candidate] = []
    for prio, (label, pattern) in enumerate(_PATTERNS):
        for m in pattern.finditer(text):
            out.append((prio, m.start(), m.end(), label))
    return out


def _resolve(candidates: list[_Candidate]) -> list[_Candidate]:
    """Greedily accept non-overlapping spans in priority order.

    Processing strictly by ascending priority reproduces "apply each regex
    pattern in order, then fill remaining gaps" — a lower-priority span is
    committed before any higher-priority span that would overlap it is seen.
    """
    accepted: list[_Candidate] = []
    for cand in sorted(candidates, key=lambda c: (c[0], c[1])):
        _, start, end, _ = cand
        if any(start < a_end and a_start < end for _, a_start, a_end, _ in accepted):
            continue
        accepted.append(cand)
    return accepted


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, labels_found)``.

    Each accepted span is replaced by ``[REDACTED:LABEL]``. ``labels_found``
    lists the kinds of PII detected (deduplicated, in detection order: regex
    pattern order first, then model labels).
    """
    candidates = _regex_candidates(text)
    for sp in model_spans(text):
        candidates.append((_MODEL_PRIORITY, sp.start, sp.end, sp.label))

    accepted = _resolve(candidates)
    if not accepted:
        return text, []

    found: list[str] = []
    for _, _, _, label in sorted(accepted, key=lambda c: (c[0], c[1])):
        if label not in found:
            found.append(label)

    # Apply replacements right-to-left so earlier offsets stay valid.
    redacted = text
    for _, start, end, label in sorted(accepted, key=lambda c: c[1], reverse=True):
        redacted = redacted[:start] + f"[REDACTED:{label}]" + redacted[end:]
    return redacted, found


def contains_pii(text: str) -> bool:
    """Cheap regex-only PII check (used for per-event sensitivity flagging).

    Deliberately regex-only: this runs in the clustering hot path, and the
    deterministic floor is the right signal for tagging a cluster sensitive.
    The model layer applies at the redaction boundary (:func:`redact`).
    """
    return any(pattern.search(text) for _, pattern in _PATTERNS)


__all__ = ["Span", "contains_pii", "model_spans", "redact"]
