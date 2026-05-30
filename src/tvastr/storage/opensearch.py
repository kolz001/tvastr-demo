"""OpenSearch-backed audit store (production path).

Indexes each :class:`~tvastr.domain.AuditRecord` so runs are queryable from the
OpenSearch dashboards. Used when ``use_mocks`` is false.
"""

from __future__ import annotations

from typing import Any

from tvastr.config import Settings
from tvastr.domain import AuditRecord
from tvastr.logging import get_logger

log = get_logger(__name__)


class OpenSearchAuditStore:
    def __init__(self, settings: Settings) -> None:
        self.index = settings.opensearch_audit_index
        self._settings = settings
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from opensearchpy import OpenSearch  # lazy import

            auth = None
            if self._settings.opensearch_password:
                auth = (self._settings.opensearch_user, self._settings.opensearch_password)
            self._client = OpenSearch(hosts=[self._settings.opensearch_url], http_auth=auth)
        return self._client

    def save(self, record: AuditRecord) -> None:
        client = self._get_client()
        client.index(index=self.index, id=record.id, body=record.model_dump(mode="json"))
        log.info("audit.save", pattern=record.pattern_id, outcome=record.outcome)

    def all(self) -> list[AuditRecord]:
        client = self._get_client()
        resp = client.search(index=self.index, body={"query": {"match_all": {}}, "size": 100})
        return [AuditRecord.model_validate(hit["_source"]) for hit in resp["hits"]["hits"]]
