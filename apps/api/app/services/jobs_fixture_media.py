"""Independent Jobs media publication for a committed owned delivery."""

from __future__ import annotations

import uuid

from yoao_jobs.publication_postgres import (
    publication_database_url,
    publish_fixture_media_delivery as publish_owned_delivery,
)

from app.config import settings
from app.services.jobs_queue import publish_queue_envelope


class _SelectedQueuePublisher:
    __slots__ = ()

    async def publish_envelope(self, envelope: dict) -> str:
        return await publish_queue_envelope(envelope)


_QUEUE_PUBLISHER = _SelectedQueuePublisher()


async def publish_fixture_media_delivery(delivery_id: uuid.UUID) -> str:
    """Publish the committed saved envelope through the distinct Jobs owner."""
    database_url = publication_database_url(
        settings.jobs_publication_database_url,
        settings.jobs_publication_database_url_file,
    )
    return await publish_owned_delivery(
        database_url,
        delivery_id,
        _QUEUE_PUBLISHER,
    )
