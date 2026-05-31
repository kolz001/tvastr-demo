"""Harvest log entries from Grafana Loki into ``LogEvent`` JSONL.

Loki is OSS (Apache-2.0), self-hostable, and widely used in the Grafana stack.
This module hits its HTTP query API (``/loki/api/v1/query_range``) with a
user-supplied LogQL query, converts each returned entry into a ``LogEvent``,
and writes a JSONL the pipeline can replay.

Mock mode returns a deterministic fixture so the demo and tests run offline
without a Loki cluster.

For non-trivial log shapes, pass a ``line_to_event`` callable that maps your
log line (raw string or parsed JSON) onto a ``LogEvent``. The default tries to
parse the line as JSON and pulls out ``service``, ``severity``, ``message``,
and ``stack_trace`` keys if present.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from tvastr.domain import LogEvent, Severity
from tvastr.logging import get_logger

log = get_logger(__name__)


LineToEvent = Callable[[str, dict[str, str]], LogEvent | None]
"""A converter from (raw line, stream labels) → LogEvent, or None to skip."""


_SEVERITY_MAP = {
    "fatal": Severity.CRITICAL,
    "crit": Severity.CRITICAL,
    "critical": Severity.CRITICAL,
    "error": Severity.ERROR,
    "err": Severity.ERROR,
    "warn": Severity.WARNING,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
    "debug": Severity.DEBUG,
}


def default_line_to_event(line: str, labels: dict[str, str]) -> LogEvent | None:
    """Best-effort default: try to parse ``line`` as JSON, else use it raw as the message.

    Picks ``service`` from labels (``service_name`` / ``app`` / ``job``) or from
    a top-level JSON field. Severity is read from a common set of field/label
    names; defaults to ERROR (we're remediation-focused, so ERROR is the prior
    when nothing else says otherwise).
    """
    service = (
        labels.get("service_name")
        or labels.get("service")
        or labels.get("app")
        or labels.get("job")
        or "unknown"
    )
    severity_raw = (labels.get("level") or labels.get("severity") or "error").lower()
    severity = _SEVERITY_MAP.get(severity_raw, Severity.ERROR)
    message = line.strip()
    stack_trace: str | None = None

    # If the line is JSON, lift fields out of it.
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        message = str(obj.get("message") or obj.get("msg") or obj.get("error") or message)
        if (s := obj.get("service") or obj.get("logger") or obj.get("module")) is not None:
            service = str(s)
        if (lvl := obj.get("level") or obj.get("severity")) is not None:
            severity = _SEVERITY_MAP.get(str(lvl).lower(), severity)
        if (tr := obj.get("stack_trace") or obj.get("exception") or obj.get("trace")) is not None:
            stack_trace = str(tr)

    if not message:
        return None

    return LogEvent(
        service=service,
        severity=severity,
        message=message[:4000],
        stack_trace=stack_trace,
        attributes={k: v for k, v in labels.items() if isinstance(v, str)},
        source="loki",
    )


def loki_entry_to_event(
    timestamp_ns: str,
    line: str,
    labels: dict[str, str],
    *,
    line_to_event: LineToEvent | None = None,
) -> LogEvent | None:
    """Convert one Loki entry into a ``LogEvent``."""
    event = (line_to_event or default_line_to_event)(line, labels)
    if event is None:
        return None
    # Override the event timestamp with Loki's (nanoseconds since epoch).
    try:
        ts = datetime.fromtimestamp(int(timestamp_ns) / 1_000_000_000, tz=UTC)
        event = event.model_copy(update={"timestamp": ts})
    except (ValueError, OverflowError):
        pass
    return event


def write_jsonl(events: Iterable[LogEvent], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(event.model_dump_json() + "\n")
            n += 1
    return n


class LokiLogFetcher:
    """Live fetcher — hits Loki's HTTP query_range endpoint."""

    def __init__(
        self,
        url: str,
        *,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password

    def fetch(
        self, *, query: str, limit: int = 500, since_hours: int = 24
    ) -> list[tuple[str, str, dict[str, str]]]:
        """Return raw entries: ``(timestamp_ns, line, stream_labels)``.

        Conversion to ``LogEvent`` happens in :func:`loki_entry_to_event` so
        callers can plug in a custom converter without re-fetching.
        """
        import httpx  # already a project dep

        end_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000)
        start_ns = end_ns - since_hours * 3600 * 1_000_000_000
        params = {
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(min(limit, 5000)),
            "direction": "backward",
        }
        auth = (self.user, self.password or "") if self.user else None

        log.info("ingest.loki.fetch", url=self.url, query=query, limit=limit)
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(f"{self.url}/loki/api/v1/query_range", params=params, auth=auth)
            resp.raise_for_status()
            payload = resp.json()

        entries: list[tuple[str, str, dict[str, str]]] = []
        for stream in (payload.get("data") or {}).get("result", []):
            labels: dict[str, str] = {
                k: str(v) for k, v in (stream.get("stream") or {}).items()
            }
            for ts_ns, line in stream.get("values", []):
                entries.append((ts_ns, line, labels))
        return entries


class MockLokiLogFetcher:
    """Deterministic fixture — three entries, two share an exception type."""

    def __init__(self, url: str = "http://localhost:3100") -> None:
        self.url = url

    def fetch(
        self, *, query: str = '{app="llamaindex-app"}', limit: int = 500, since_hours: int = 24
    ) -> list[tuple[str, str, dict[str, str]]]:
        log.info("ingest.loki.fetch", url=self.url, query=query, mocked=True)
        base_ns = int(datetime(2026, 5, 28, 12, 0, tzinfo=UTC).timestamp() * 1_000_000_000)
        labels = {"app": "llamaindex-app", "level": "error"}
        entries = [
            (
                str(base_ns),
                json.dumps(
                    {
                        "level": "error",
                        "service": "llamaindex-app",
                        "message": (
                            "ModuleNotFoundError: No module named 'llama_index.llms.openai'"
                        ),
                    }
                ),
                labels,
            ),
            (
                str(base_ns + 60 * 1_000_000_000),
                json.dumps(
                    {
                        "level": "error",
                        "service": "llamaindex-app",
                        "message": (
                            "ModuleNotFoundError: No module named 'llama_index.llms.openai'"
                        ),
                    }
                ),
                labels,
            ),
            (
                str(base_ns + 120 * 1_000_000_000),
                json.dumps(
                    {
                        "level": "error",
                        "service": "llamaindex-vector-store",
                        "message": (
                            "ValueError: Embedding dimension 1536 does not match "
                            "collection dimension 768"
                        ),
                    }
                ),
                {"app": "llamaindex-app", "level": "error"},
            ),
        ]
        return entries[:limit]


def harvest_loki_to_jsonl(
    url: str | None,
    query: str,
    out_path: Path,
    *,
    user: str | None = None,
    password: str | None = None,
    limit: int = 500,
    since_hours: int = 24,
    use_mocks: bool = False,
    line_to_event: LineToEvent | None = None,
) -> tuple[int, int]:
    """Fetch → convert → write. Returns ``(entries_seen, events_written)``."""
    if use_mocks or not url:
        fetcher: MockLokiLogFetcher | LokiLogFetcher = MockLokiLogFetcher(url or "")
    else:
        fetcher = LokiLogFetcher(url, user=user, password=password)

    raw = fetcher.fetch(query=query, limit=limit, since_hours=since_hours)
    events: list[LogEvent] = []
    for ts_ns, line, labels in raw:
        event = loki_entry_to_event(ts_ns, line, labels, line_to_event=line_to_event)
        if event is not None:
            events.append(event)

    n = write_jsonl(events, out_path)
    log.info(
        "ingest.loki.done",
        url=url,
        out=str(out_path),
        entries=len(raw),
        events=n,
    )
    return len(raw), n
