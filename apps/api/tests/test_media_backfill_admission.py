"""Durable dataset media backfill admission surface."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import datasets
from app.schemas.dataset import DatasetImageBackfillResponse


def _job(*, status: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        payload={
            "dataset_id": str(uuid.uuid4()),
            "purpose": "thumbnail",
            "accepted_before": datetime.now(timezone.utc).isoformat(),
            "operation_id": str(uuid.uuid4()),
        },
    )


@pytest.mark.parametrize(
    ("owner_status", "expected"),
    [
        ("pending", "pending"),
        ("running", "running"),
        ("completed", "ready"),
        ("failed", "failed"),
        ("cancelled", "canceled"),
    ],
)
def test_media_backfill_response_preserves_durable_status(owner_status, expected):
    response = datasets._media_backfill_response(_job(status=owner_status))
    assert isinstance(response, DatasetImageBackfillResponse)
    assert response.status == expected
    assert response.purpose == "thumbnail"


def test_media_backfill_response_rejects_unknown_receipt():
    job = _job()
    job.payload = {"dataset_id": str(uuid.uuid4())}
    with pytest.raises(HTTPException) as error:
        datasets._media_backfill_response(job)
    assert error.value.status_code == 502
