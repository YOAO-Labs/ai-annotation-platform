"""Durable dataset media backfill admission surface."""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["uncertain_io", "cancelled"])
async def test_uncertain_backfill_keeps_its_job_anchor(
    test_engine, monkeypatch, interruption
):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.services import async_job
    from app.services.content_snapshot import ContentError
    from app.workers import media

    assert (test_engine.url.database or "").endswith(("_test", "_e2e"))
    monkeypatch.setattr(
        settings, "database_url", test_engine.url.render_as_string(hide_password=False)
    )
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        job = await async_job.create_job(
            db,
            kind="media_backfill",
            payload={
                "dataset_id": str(uuid.uuid4()),
                "purpose": "thumbnail",
                "accepted_before": datetime.now(timezone.utc).isoformat(),
                "operation_id": str(uuid.uuid4()),
                "media_backfill_kind": "aap-media-backfill/v1",
                "video_delegations": {},
            },
        )
        job_id = job.id

    if interruption == "cancelled":
        failure = asyncio.CancelledError()
    else:
        failure = ContentError("storage_unavailable", 503)
        failure.storage_operation_unsettled = True

    async def uncertain(*args, **kwargs):
        raise failure

    monkeypatch.setattr(media, "_backfill_media", uncertain)
    try:
        with pytest.raises(type(failure)) as caught:
            await media._run_media_backfill_job(job_id)
        assert caught.value is failure
        async with sessions() as db:
            state = await db.scalar(
                select(AsyncJob.status).where(AsyncJob.id == job_id)
            )
            assert state == "running"
    finally:
        async with sessions.begin() as db:
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
