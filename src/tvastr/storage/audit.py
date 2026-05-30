"""Audit store interface and the in-memory implementation used for local dev."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tvastr.config import Settings
from tvastr.domain import AuditRecord
from tvastr.logging import get_logger

log = get_logger(__name__)


@runtime_checkable
class AuditStore(Protocol):
    def save(self, record: AuditRecord) -> None: ...
    def all(self) -> list[AuditRecord]: ...


class InMemoryAuditStore:
    """Keeps audit records in a list — perfect for tests, demos, and offline runs."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def save(self, record: AuditRecord) -> None:
        self._records.append(record)
        log.info("audit.save", pattern=record.pattern_id, outcome=record.outcome, mocked=True)

    def all(self) -> list[AuditRecord]:
        return list(self._records)


def build_audit_store(settings: Settings) -> AuditStore:
    if settings.use_mocks:
        return InMemoryAuditStore()
    from tvastr.storage.opensearch import OpenSearchAuditStore

    return OpenSearchAuditStore(settings)
