"""Replays sample failure logs from a JSON Lines file.

Each line is a JSON object matching :class:`~tvastr.domain.LogEvent`. This is the
default source for local development and the demo, and doubles as the fixture set
for the agent's test cases.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tvastr.domain import LogEvent
from tvastr.ingestion.base import parse_jsonl_events
from tvastr.logging import get_logger

log = get_logger(__name__)


def default_sample_path() -> Path:
    """Path to the bundled sample LlamaIndex failure logs."""
    return (
        Path(__file__).resolve().parents[3]
        / "data"
        / "sample_logs"
        / "llamaindex_failures.jsonl"
    )


class SimulatedLogSource:
    name = "simulated"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_sample_path()

    def read(self) -> Iterable[LogEvent]:
        if not self.path.exists():
            raise FileNotFoundError(f"Sample log file not found: {self.path}")
        log.info("ingest.read", source=self.name, path=str(self.path))
        with self.path.open("r", encoding="utf-8") as fh:
            yield from parse_jsonl_events(fh, source_name=self.name)
