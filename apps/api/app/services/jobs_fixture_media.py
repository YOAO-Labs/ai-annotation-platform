"""Independent Jobs media publication for a committed owned delivery."""

from __future__ import annotations

import uuid

from yoao_jobs.publication_postgres import (
    publication_database_url,
    publish_fixture_media_delivery as publish_owned_delivery,
)
from yoao_jobs.queue_client import JobsQueueClient
from yoao_jobs.queue_contract import JobsQueueUnavailable

from app.config import settings
from app.services.jobs_queue import _jobs_queue_token


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
