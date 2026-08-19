"""Continuous self-log capture: the foundation the self-healing loop reads from.

``SelfLogWriter`` is a structlog processor that mirrors every log event tvastr
emits to a dated JSONL file under a self-log directory, independent of the
console/JSON renderer wired up by :func:`tvastr.logging.configure_logging`.
Later self-heal stages (Task 2 onward) mine these files to detect tvastr's own
recurring failures and feed them back through the same diagnose/fix/verify
pipeline used for the target repo.

This module must never destabilize application logging: a full disk, an
unwritable directory, or any other I/O failure is swallowed silently and
``event_dict`` is always returned unchanged, so a broken self-log never takes
down (or even warns) the rest of the app.
"""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from structlog.typing import EventDict, WrappedLogger


def selflog_path(dir: Path, day: str) -> Path:
    """Path to the self-log file for a given UTC date (``YYYY-MM-DD``).

    Task 2's mining stage reads files through this same helper so the naming
    convention only lives in one place.
    """
    return dir / f"tvastr-{day}.jsonl"


class SelfLogWriter:
    """structlog processor: append every event to today's dated JSONL file.

    Thread-safe (a single lock guards the read-day / write / prune sequence)
    and best-effort: any exception raised while writing or pruning is
    swallowed so a self-log problem can never break application logging.
    """

    def __init__(self, dir: Path, retention_days: int = 30) -> None:
        self.dir = dir
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._last_day: str | None = None

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        with contextlib.suppress(Exception):
            self._write(event_dict)
        return event_dict

    def _write(self, event_dict: EventDict) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        line = json.dumps(event_dict, default=str) + "\n"
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = selflog_path(self.dir, day)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
            if day != self._last_day:
                self._last_day = day
                self._prune(day)

    def _prune(self, today: str) -> None:
        cutoff = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC) - timedelta(
            days=self.retention_days
        )
        for path in self.dir.glob("tvastr-*.jsonl"):
            day_part = path.stem.removeprefix("tvastr-")
            try:
                file_day = datetime.strptime(day_part, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if file_day < cutoff:
                path.unlink(missing_ok=True)
