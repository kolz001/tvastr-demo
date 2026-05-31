"""Audit store interface and the in-memory implementation used for tests."""

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
    """Keeps audit records in a list — perfect for tests and ephemeral runs."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def save(self, record: AuditRecord) -> None:
        self._records.append(record)
        log.info(
            "audit.save",
            pattern=record.pattern_id,
            outcome=record.outcome,
            backend="memory",
        )

    def all(self) -> list[AuditRecord]:
        return list(self._records)


def build_audit_store(settings: Settings) -> AuditStore:
    """Build the audit store selected by ``settings.audit_backend``.

    Decoupled from ``use_mocks`` so a user running with mock LLM/GitHub clients
    can still get persistent audit on disk — and conversely, a live run can opt
    into an ephemeral in-memory store for ad-hoc debugging.
    """
    backend = settings.audit_backend
    if backend == "memory":
        return InMemoryAuditStore()
    if backend == "file":
        from tvastr.storage.file_audit import FileAuditStore

        return FileAuditStore(settings.audit_file_path)
    if backend == "opensearch":
        from tvastr.storage.opensearch import OpenSearchAuditStore

        return OpenSearchAuditStore(settings)
    raise ValueError(f"Unknown audit backend: {backend!r}")
