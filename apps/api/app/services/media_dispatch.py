"""Prepare real media deliveries in the caller's unit of work."""

import json
import uuid

from yoao_jobs.queue_contract import encode_dispatch_request

from app.services.jobs_dispatch import jobs_dispatch_data


async def prepare(
    db,
    *,
    owner_kind,
    owner_id,
    producer_kind,
    source,
    task_name,
    args,
    kwargs=None,
    delivery_id=None,
):
    delivery_id = uuid.uuid4() if delivery_id is None else uuid.UUID(str(delivery_id))
    queue = "image-pyramid" if producer_kind == "pyramid" else "media"
    envelope = json.loads(
        encode_dispatch_request(
            task_name,
            str(delivery_id),
            args,
            kwargs=kwargs or {},
            options={"queue": queue},
        )
    )
    return await jobs_dispatch_data(db).prepare_media(
        owner_kind=owner_kind,
        owner_id=owner_id,
        producer_kind=producer_kind,
        delivery_id=delivery_id,
        source=source,
        envelope=envelope,
    )


async def publish(prepared, ordinary):
    if prepared.ownership == "fixture":
        from app.services.jobs_fixture_media import publish_fixture_media_delivery

        return await publish_fixture_media_delivery(prepared.delivery_id)
    return str(ordinary().id)
