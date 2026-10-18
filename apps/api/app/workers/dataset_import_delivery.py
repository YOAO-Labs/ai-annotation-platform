"""Retry Assets import delivery handoffs after their business commit."""

from __future__ import annotations

import asyncio
import logging
import uuid

from aap_assets_source.import_delivery_contract import DatasetImportDelivery
from aap_assets_source.ingest_contract import IngestDeliveryLease
from aap_assets_media.contract import MetadataSpec, ScheduleWorkRequest

from app.services.dataset_import_delivery import (
    claim_dataset_import_delivery,
    complete_dataset_import_delivery,
    dataset_import_delivery_target_is_current,
)
from app.services.dataset_source_ingest_delivery import (
    claim_dataset_source_ingest_delivery,
    complete_dataset_source_ingest_delivery,
)
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.assets_media import media_gateway
from app.services.notification import NotificationService
from app.workers._db import task_session
from app.workers.celery_app import celery_app
from app.workers.media_dispatch import CeleryMediaJobDispatcher

log = logging.getLogger(__name__)

DELIVERY_LEASE_SECONDS = 120
MAX_DELIVERIES_PER_RUN = 100


async def _deliver_one(
    db,
    delivery: DatasetImportDelivery,
    dispatcher: MediaJobDispatcher,
) -> str:
    if delivery.kind in {"thumbnail", "video_metadata"}:
        is_current = await dataset_import_delivery_target_is_current(db, delivery)
        await db.commit()
        if not is_current:
            await complete_dataset_import_delivery(
                db,
                delivery_id=delivery.delivery_id,
                lease_token=delivery.lease_token,
            )
            await db.commit()
            return "stale"

        if delivery.kind == "thumbnail":
            dispatcher.dispatch_thumbnail(delivery.target_id)
        else:
            async with media_gateway(db) as media:
                await media.call(
                    "schedule_video_media",
                    ScheduleWorkRequest(
                        operation_id=delivery.delivery_id,
                        dataset_item_id=delivery.target_id,
                        spec=MetadataSpec(),
                    ),
                )
    else:
        published = await NotificationService(db).publish_by_id(delivery.target_id)
        await db.commit()
        if not published:
            await complete_dataset_import_delivery(
                db,
                delivery_id=delivery.delivery_id,
                lease_token=delivery.lease_token,
            )
            await db.commit()
            return "stale"

    await complete_dataset_import_delivery(
        db,
        delivery_id=delivery.delivery_id,
        lease_token=delivery.lease_token,
    )
    await db.commit()
    return "delivered"


async def _deliver_source_ingest_one(
    db,
    delivery: IngestDeliveryLease,
    dispatcher: MediaJobDispatcher,
) -> str:
    is_current = await dataset_import_delivery_target_is_current(db, delivery)
    await db.commit()
    if is_current:
        if delivery.kind == "thumbnail":
            dispatcher.dispatch_thumbnail(delivery.target_id)
        else:
            async with media_gateway(db) as media:
                await media.call(
                    "schedule_video_media",
                    ScheduleWorkRequest(
                        operation_id=delivery.id,
                        dataset_item_id=delivery.target_id,
                        spec=MetadataSpec(),
                    ),
                )
    await complete_dataset_source_ingest_delivery(
        db, delivery_id=delivery.id, lease_token=delivery.lease_token
    )
    await db.commit()
    return "delivered" if is_current else "stale"


async def _relay_pending() -> dict[str, int]:
    totals = {"claimed": 0, "delivered": 0, "stale": 0, "deferred": 0}
    dispatcher = CeleryMediaJobDispatcher()
    queues = (
        (claim_dataset_import_delivery, _deliver_one),
        (claim_dataset_source_ingest_delivery, _deliver_source_ingest_one),
    )
    drained = set()
    turn = 0

    async with task_session() as db:
        while totals["claimed"] < MAX_DELIVERIES_PER_RUN and len(drained) < len(queues):
            index = turn % len(queues)
            turn += 1
            if index in drained:
                continue
            claim, deliver = queues[index]
            lease_token = uuid.uuid4()
            try:
                delivery = await claim(
                    db,
                    lease_token=lease_token,
                    lease_seconds=DELIVERY_LEASE_SECONDS,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                log.exception("dataset import delivery claim failed queue=%s", index)
                drained.add(index)
                continue
            if delivery is None:
                drained.add(index)
                continue

            totals["claimed"] += 1
            try:
                outcome = await deliver(db, delivery, dispatcher)
            except Exception:
                await db.rollback()
                totals["deferred"] += 1
                log.exception(
                    "dataset import delivery deferred delivery_id=%s kind=%s target_id=%s",
                    delivery.id
                    if isinstance(delivery, IngestDeliveryLease)
                    else delivery.delivery_id,
                    delivery.kind,
                    delivery.target_id,
                )
                continue
            totals[outcome] += 1

    return totals


@celery_app.task(
    name="app.workers.dataset_import_delivery.relay_dataset_import_deliveries",
    queue="maintenance",
    max_retries=0,
)
def relay_dataset_import_deliveries() -> dict[str, int]:
    return asyncio.run(_relay_pending())
