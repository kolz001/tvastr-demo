"""Storage layer — persists audit records (and raw logs) to OpenSearch.

The audit trail is first-class: every remediation run records where data flowed
(local vs cloud) and what the agent did, so the system is reviewable and compliant.
"""

from tvastr.storage.audit import AuditStore, InMemoryAuditStore, build_audit_store
from tvastr.storage.opensearch import OpenSearchAuditStore

__all__ = ["AuditStore", "InMemoryAuditStore", "OpenSearchAuditStore", "build_audit_store"]
