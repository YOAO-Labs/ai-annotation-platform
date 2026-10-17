"""Client operations for administrative health, audit-adjacent and AI queues."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.admin_operation_models import (
    AliasFrequencyResponse,
    AnalyticsPanelName,
    AnalyticsPanelResponse,
    PreannotateBulkClearRequest,
    PreannotateBulkClearResponse,
    PreannotateProjectSummaryResponse,
    PreannotateQueueResponse,
    SystemHealthResponse,
)

IdLike = str | UUID


class AdminOperations:
    """Stable admin endpoints; server-side role and project scope remain authoritative."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def preannotate_queue(self, *, limit: int = 50) -> PreannotateQueueResponse:
        """List completed preannotation batches in the caller's permitted scope."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        response = self._http.request(
            "GET", "/admin/preannotate-queue", params={"limit": limit}
        )
        return PreannotateQueueResponse.model_validate(response.json())

    def bulk_clear_preannotate(
        self, body: PreannotateBulkClearRequest
    ) -> PreannotateBulkClearResponse:
        """Clear predictions or reset up to 50 batches; result can be partially successful."""
        response = self._http.request(
            "POST",
            "/admin/preannotate-queue/bulk-clear",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return PreannotateBulkClearResponse.model_validate(response.json())

    def preannotate_summary(self) -> PreannotateProjectSummaryResponse:
        response = self._http.request("GET", "/admin/preannotate-summary")
        return PreannotateProjectSummaryResponse.model_validate(response.json())

    def system_health(self) -> SystemHealthResponse:
        """Check PostgreSQL, Redis, MinIO, Celery queues and worker heartbeats."""
        response = self._http.request("GET", "/admin/system-health")
        return SystemHealthResponse.model_validate(response.json())

    def analytics(
        self,
        panel_name: AnalyticsPanelName,
        *,
        days: int = 30,
    ) -> AnalyticsPanelResponse:
        """Read one fixed DuckDB analytics panel; missing offline data returns 503."""
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("days must be an integer between 1 and 365")
        response = self._http.request(
            "GET", f"/admin/analytics/{panel_name}", params={"days": days}
        )
        return AnalyticsPanelResponse.model_validate(response.json())

    def alias_frequency(self, project_id: IdLike) -> AliasFrequencyResponse:
        """Read the capped prediction-label frequency for an authorized project."""
        response = self._http.request(
            "GET", f"/admin/projects/{project_id}/alias-frequency"
        )
        return AliasFrequencyResponse.model_validate(response.json())
