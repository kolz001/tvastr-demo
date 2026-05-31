"""File-backed audit store — append-only JSONL.

Zero infrastructure: just a path on disk. Good for laptop / portfolio / hobby
deployments where standing up OpenSearch is overkill. Each ``AuditRecord`` is
appended as one JSON line; ``all()`` reads the file back. Concurrent writers
are not supported — single-process append only.
"""

from __future__ import annotations

import json
from pathlib import Path

from tvastr.domain import AuditRecord
from tvastr.logging import get_logger

log = get_logger(__name__)


class FileAuditStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, record: AuditRecord) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
        log.info(
            "audit.save",
            pattern=record.pattern_id,
            outcome=record.outcome,
            backend="file",
            path=str(self.path),
        )

    def all(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        records: list[AuditRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(AuditRecord.model_validate(json.loads(stripped)))
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning("audit.skip_malformed", path=str(self.path), error=str(exc))
        return records
