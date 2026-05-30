"""Replays sample failure logs from a JSON Lines file.

Each line is a JSON object matching :class:`~tvastr.domain.LogEvent`. This is the
default source for local development and the demo, and doubles as the fixture set
for the agent's test cases.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from tvastr.domain import LogEvent
from tvastr.logging import get_logger

log = get_logger(__name__)


def default_sample_path() -> Path:
    """Path to the bundled sample Haystack failure logs."""
    return Path(__file__).resolve().parents[3] / "data" / "sample_logs" / "haystack_failures.jsonl"


class SimulatedLogSource:
    name = "simulated"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_sample_path()

    def read(self) -> Iterable[LogEvent]:
        if not self.path.exists():
            raise FileNotFoundError(f"Sample log file not found: {self.path}")
        log.info("ingest.read", source=self.name, path=str(self.path))
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw or raw.startswith("#"):
                    continue
                try:
                    yield LogEvent.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning("ingest.skip_malformed", line=line_no, error=str(exc))
