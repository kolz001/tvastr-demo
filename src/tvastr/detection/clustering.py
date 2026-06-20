"""Group similar log events into failure clusters.

Clustering is fingerprint-based: each message is normalized (variable tokens like
ids, numbers, hex, quoted literals stripped) so that the same failure with
different runtime values collapses into one pattern.
"""

from __future__ import annotations

import hashlib
import re

from tvastr.domain import FailurePattern, LogEvent

_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{6,}\b"), "<hex>"),
    # Mixed alphanumeric identifiers (>=4 chars with at least one letter and one
    # digit) — e.g. short doc ids like "4f9a2" — so the same failure with different
    # runtime ids collapses into one fingerprint.
    (
        re.compile(
            r"\b(?=[0-9A-Za-z]{4,}\b)(?=[0-9A-Za-z]*[A-Za-z])(?=[0-9A-Za-z]*[0-9])[0-9A-Za-z]+\b"
        ),
        "<id>",
    ),
    (re.compile(r"\b\d+\b"), "<num>"),
    (re.compile(r"'[^']*'"), "'<str>'"),
    (re.compile(r'"[^"]*"'), '"<str>"'),
    (re.compile(r"\s+"), " "),
]

_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning))\b")


def _normalize(message: str) -> str:
    text = message.strip()
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.lower()


def _exception_type(event: LogEvent) -> str | None:
    for text in (event.message, event.stack_trace or ""):
        match = _EXCEPTION_RE.search(text)
        if match:
            return match.group(1)
    return None


def fingerprint(event: LogEvent) -> str:
    """Stable hash identifying the failure class of an event."""
    exc = _exception_type(event) or "NoException"
    basis = f"{event.service}|{exc}|{_normalize(event.message)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _title(event: LogEvent, exc: str | None) -> str:
    # Without an exception type the head alone is too generic (every synthetic
    # "UnexpectedBehavior: <issue title>" event would share one title), so keep
    # the full message to stay distinguishable in the UI and PR titles.
    base = exc or event.message
    return f"{base} in {event.service}"[:120]


def cluster_events(events: list[LogEvent]) -> list[FailurePattern]:
    """Collapse events into failure patterns, ordered by frequency (desc)."""
    clusters: dict[str, FailurePattern] = {}
    for event in events:
        fp = fingerprint(event)
        exc = _exception_type(event)
        cluster = clusters.get(fp)
        if cluster is None:
            clusters[fp] = FailurePattern(
                fingerprint=fp,
                title=_title(event, exc),
                representative_message=event.message,
                exception_type=exc,
                count=1,
                first_seen=event.timestamp,
                last_seen=event.timestamp,
                sample_event_ids=[event.id],
            )
            continue
        cluster.count += 1
        cluster.first_seen = min(cluster.first_seen, event.timestamp)
        cluster.last_seen = max(cluster.last_seen, event.timestamp)
        if len(cluster.sample_event_ids) < 5:
            cluster.sample_event_ids.append(event.id)

    return sorted(clusters.values(), key=lambda c: c.count, reverse=True)
