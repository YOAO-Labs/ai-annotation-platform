from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.admin_operation_models import (
    AliasFrequencyResponse,
    AnalyticsPanelResponse,
    PreannotateBulkClearRequest,
    PreannotateBulkClearResponse,
    PreannotateProjectSummaryResponse,
    PreannotateQueueResponse,
    SystemHealthResponse,
)
from ai_annotation.admin_operations import AdminOperations

from .conftest import API


def _resource(client) -> AdminOperations:
    return AdminOperations(client._http)


def test_preannotate_queue_and_summary_are_typed(client, respx_mock):
    project_id, batch_id = uuid4(), uuid4()
    queue = respx_mock.get(f"{API}/admin/preannotate-queue").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "batch_id": str(batch_id),
                        "batch_name": "ready",
                        "batch_status": "pre_annotated",
                        "project_id": str(project_id),
                        "project_name": "owned project",
                        "total_tasks": 5,
                        "prediction_count": 4,
                        "failed_count": 1,
                        "last_run_at": "2026-10-17T00:00:00Z",
                        "can_retry": True,
                    }
                ]
            },
        )
    )
    summary = respx_mock.get(f"{API}/admin/preannotate-summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "project_id": str(project_id),
                        "project_name": "owned project",
                        "type_key": "detect",
                        "ready_batches": 1,
                        "active_batches": 2,
                        "recent_failures": 1,
                    }
                ]
            },
        )
    )

    resource = _resource(client)
    queue_result = resource.preannotate_queue(limit=17)
    summary_result = resource.preannotate_summary()

    assert isinstance(queue_result, PreannotateQueueResponse)
    assert queue_result.items[0].batch_id == batch_id
    assert queue_result.items[0].can_retry is True
    assert isinstance(summary_result, PreannotateProjectSummaryResponse)
    assert summary_result.items[0].active_batches == 2
    assert queue.calls.last.request.url.params["limit"] == "17"
    assert summary.called


def test_bulk_clear_request_is_bounded_and_response_preserves_partial_results(
    client, respx_mock
):
    batch_id, skipped_id = uuid4(), uuid4()
    route = respx_mock.post(f"{API}/admin/preannotate-queue/bulk-clear").mock(
        return_value=httpx.Response(
            200,
            json={
                "succeeded": [str(batch_id)],
                "skipped": [{"batch_id": str(skipped_id), "reason": "forbidden"}],
                "failed": [],
            },
        )
    )
    body = PreannotateBulkClearRequest(
        batch_ids=[batch_id, skipped_id],
        mode="reset_to_draft",
        reason="operator requested reset",
    )

    result = _resource(client).bulk_clear_preannotate(body)

    assert isinstance(result, PreannotateBulkClearResponse)
    assert result.succeeded == [batch_id]
    assert result.skipped[0].reason == "forbidden"
    assert json.loads(route.calls.last.request.content) == {
        "batch_ids": [str(batch_id), str(skipped_id)],
        "mode": "reset_to_draft",
        "reason": "operator requested reset",
    }
    with pytest.raises(ValidationError):
        PreannotateBulkClearRequest(batch_ids=[], reason="long enough reason")
    with pytest.raises(ValidationError):
        PreannotateBulkClearRequest(batch_ids=[batch_id], reason="short")


def test_health_analytics_and_alias_frequency(client, respx_mock):
    project_id = uuid4()
    health = respx_mock.get(f"{API}/admin/system-health").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "degraded",
                "version": "0.26.1",
                "components": [
                    {
                        "name": "redis",
                        "label": "Redis",
                        "status": "degraded",
                        "latency_ms": 5,
                    }
                ],
                "celery": {
                    "active_count": 1,
                    "workers": [],
                    "queues": [{"name": "default", "length": 0, "status": "ok"}],
                },
            },
        )
    )
    analytics = respx_mock.get(f"{API}/admin/analytics/throughput_daily").mock(
        return_value=httpx.Response(200, json={"panel": "throughput_daily", "data": []})
    )
    aliases = respx_mock.get(f"{API}/admin/projects/{project_id}/alias-frequency").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_id": str(project_id),
                "total_predictions": 2,
                "frequency": {"car": 2},
                "last_computed_at": "2026-10-17T00:00:00Z",
            },
        )
    )

    resource = _resource(client)
    health_result = resource.system_health()
    analytics_result = resource.analytics("throughput_daily", days=7)
    alias_result = resource.alias_frequency(project_id)

    assert isinstance(health_result, SystemHealthResponse)
    assert health_result.celery.active_count == 1
    assert isinstance(analytics_result, AnalyticsPanelResponse)
    assert analytics_result.data == []
    assert analytics.calls.last.request.url.params["days"] == "7"
    assert isinstance(alias_result, AliasFrequencyResponse)
    assert alias_result.frequency == {"car": 2}
    assert health.called and aliases.called


def test_admin_query_bounds_fail_before_http(client, respx_mock):
    with pytest.raises(ValueError):
        _resource(client).preannotate_queue(limit=201)
    with pytest.raises(ValueError):
        _resource(client).analytics("throughput_daily", days=0)
    assert not respx_mock.calls
