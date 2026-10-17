from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import ConflictError
from ai_annotation.failed_prediction_models import (
    FailedPredictionDismissReceipt,
    FailedPredictionPage,
    FailedPredictionRetryReceipt,
)
from ai_annotation.failed_predictions import FailedPredictions

from .conftest import API


def test_failed_prediction_list_and_mutation_receipts(client, respx_mock):
    failed_id = uuid4()
    project_id = uuid4()
    task_id = uuid4()
    listed = respx_mock.get(f"{API}/admin/failed-predictions").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": str(failed_id),
                        "task_id": str(task_id),
                        "task_display_id": "T-7",
                        "project_id": str(project_id),
                        "project_name": "Project",
                        "ml_backend_id": None,
                        "backend_name": None,
                        "model_version": "v2",
                        "error_type": "timeout",
                        "message": "backend timeout",
                        "retry_count": 1,
                        "last_retry_at": None,
                        "dismissed_at": None,
                        "created_at": "2026-10-16T00:00:00Z",
                    }
                ],
                "total": 1,
                "page": 2,
                "page_size": 25,
            },
        )
    )
    retry = respx_mock.post(f"{API}/admin/failed-predictions/{failed_id}/retry").mock(
        return_value=httpx.Response(
            202, json={"status": "queued", "failed_id": str(failed_id)}
        )
    )
    dismiss = respx_mock.post(
        f"{API}/admin/failed-predictions/{failed_id}/dismiss"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "dismissed",
                "failed_id": str(failed_id),
                "dismissed_at": "2026-10-17T00:00:00Z",
            },
        )
    )
    restore = respx_mock.post(
        f"{API}/admin/failed-predictions/{failed_id}/restore"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"status": "restored", "failed_id": str(failed_id)},
        )
    )

    resource = FailedPredictions(client._http)
    page = resource.list(page=2, page_size=25, include_dismissed=True)
    queued = resource.retry(failed_id)
    dismissed = resource.dismiss(failed_id)
    restored = resource.restore(failed_id)

    assert isinstance(page, FailedPredictionPage)
    assert page.items[0].id == failed_id
    assert page.items[0].project_id == project_id
    assert listed.calls.last.request.url.params["include_dismissed"] == "true"
    assert isinstance(queued, FailedPredictionRetryReceipt)
    assert queued.status == "queued"
    assert retry.calls.call_count == 1
    assert isinstance(dismissed, FailedPredictionDismissReceipt)
    assert dismissed.dismissed_at is not None
    assert restored.status == "restored" and restored.dismissed_at is None
    assert dismiss.calls.call_count == restore.calls.call_count == 1


def test_failed_prediction_retry_conflict_is_never_retried(client, respx_mock):
    failed_id = uuid4()
    route = respx_mock.post(f"{API}/admin/failed-predictions/{failed_id}/retry").mock(
        return_value=httpx.Response(409, json={"detail": "Retry already queued"})
    )

    with pytest.raises(ConflictError):
        FailedPredictions(client._http).retry(failed_id)
    assert route.calls.call_count == 1


def test_failed_prediction_page_bounds_fail_before_http(client, respx_mock):
    resource = FailedPredictions(client._http)
    with pytest.raises(ValueError, match="page must"):
        resource.list(page=0)
    with pytest.raises(ValueError, match="page_size"):
        resource.list(page_size=201)
    assert not respx_mock.calls
