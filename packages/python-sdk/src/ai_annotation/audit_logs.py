"""Scoped audit reads and explicit one-shot attachment export."""

from __future__ import annotations

from pathlib import Path

from ai_annotation._http import HttpTransport
from ai_annotation.audit_models import (
    AuditArchiveOut,
    AuditArchiveQuery,
    AuditArchiveRowsOut,
    AuditExportQuery,
    AuditLogList,
    AuditLogQuery,
    AuditMonthlySummary,
)


class AuditLogs:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, query: AuditLogQuery | None = None) -> AuditLogList:
        query = query or AuditLogQuery()
        response = self._http.request(
            "GET",
            "/audit-logs",
            params=query.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return AuditLogList.model_validate(response.json())

    def monthly_summary(
        self, month: str, *, business_only: bool = True
    ) -> AuditMonthlySummary:
        response = self._http.request(
            "GET",
            "/audit-logs/monthly-summary",
            params={"month": month, "business_only": business_only},
        )
        return AuditMonthlySummary.model_validate(response.json())

    def archives(self) -> list[AuditArchiveOut]:
        response = self._http.request("GET", "/audit-logs/archives")
        return [AuditArchiveOut.model_validate(item) for item in response.json()]

    def archive(
        self, year: int, month: int, *, limit: int = 100, offset: int = 0
    ) -> AuditArchiveRowsOut:
        query = AuditArchiveQuery(year=year, month=month, limit=limit, offset=offset)
        year, month = query.year, query.month
        response = self._http.request(
            "GET",
            f"/audit-logs/archives/{year}/{month}",
            params={"limit": query.limit, "offset": query.offset},
        )
        return AuditArchiveRowsOut.model_validate(response.json())

    def export(
        self, dest_path: str | Path, query: AuditExportQuery | None = None
    ) -> Path:
        """Stream CSV/JSON once; the API records an export audit event."""
        query = query or AuditExportQuery()
        return self._http.stream_download(
            "/api/v1/audit-logs/export",
            Path(dest_path),
            params=query.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
