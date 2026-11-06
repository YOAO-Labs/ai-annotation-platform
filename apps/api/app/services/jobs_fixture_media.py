"""Caller-transaction preparation and independent Jobs media publication."""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import text
from yoao_jobs.publication_postgres import (
    publication_database_url,
    publish_fixture_media_delivery as publish_owned_delivery,
)
from yoao_jobs.queue_client import JobsQueueClient
from yoao_jobs.queue_contract import JobsQueueUnavailable, encode_queue_request

from app.config import settings
from app.services.jobs_queue import _jobs_queue_token


async def prepare_fixture_media_delivery(
    db,
    *,
    run_id: uuid.UUID,
    dataset_id: uuid.UUID | None,
    item_id: uuid.UUID | None,
    owner_kind: str,
    owner_id: uuid.UUID,
    producer_kind: str,
    delivery_id: uuid.UUID,
    queue: str,
    source: dict,
    envelope: dict,
) -> dict:
    """Bind the exact subject and payload in the caller's uncommitted transaction."""
    wire = json.loads(encode_queue_request(envelope))
    result = await db.scalar(
        text(
            "SELECT aap_jobs_v1.prepare_fixture_media_delivery("
            ":run, :dataset, :item, :owner_kind, :owner_id, :producer, :delivery, "
            ":queue, CAST(:source AS jsonb), CAST(:envelope AS jsonb))"
        ),
        {
            "run": run_id,
            "dataset": dataset_id,
            "item": item_id,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "producer": producer_kind,
            "delivery": delivery_id,
            "queue": queue,
            "source": json.dumps(source, ensure_ascii=False, allow_nan=False),
            "envelope": json.dumps(wire, ensure_ascii=False, allow_nan=False),
        },
    )
    if (
        not isinstance(result, dict)
        or set(result) != {"schema", "delivery_id", "payload_digest", "envelope"}
        or result["schema"] != "aap-jobs-fixture-media-publication/v1"
        or result["delivery_id"] != str(delivery_id)
        or not isinstance(result["payload_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", result["payload_digest"]) is None
        or result["envelope"] != wire
    ):
        raise RuntimeError("Jobs fixture media preparation returned an invalid receipt")
    return result


async def publish_fixture_media_delivery(delivery_id: uuid.UUID) -> str:
    """Publish the committed saved envelope through the distinct Jobs owner."""
    if not settings.jobs_queue_url:
        raise JobsQueueUnavailable()
    database_url = publication_database_url(
        settings.jobs_publication_database_url,
        settings.jobs_publication_database_url_file,
    )
    return await publish_owned_delivery(
        database_url,
        delivery_id,
        JobsQueueClient(settings.jobs_queue_url, _jobs_queue_token()),
    )
