"""Retry connector-import delivery handoffs after their business commit."""

from __future__ import annotations

import asyncio
import logging
import uuid

from aap_assets_source.import_delivery_contract import DatasetImportDelivery

from app.services.dataset_import_delivery import (
    claim_dataset_import_delivery,
    complete_dataset_import_delivery,
    dataset_import_delivery_target_is_current,
)
from app.services.media_job_dispatch import MediaJobDispatcher
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
            dispatcher.dispatch_video_metadata(delivery.target_id)
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


async def _relay_pending() -> dict[str, int]:
    totals = {"claimed": 0, "delivered": 0, "stale": 0, "deferred": 0}
    dispatcher = CeleryMediaJobDispatcher()

    async with task_session() as db:
        for _ in range(MAX_DELIVERIES_PER_RUN):
            lease_token = uuid.uuid4()
            try:
                delivery = await claim_dataset_import_delivery(
                    db,
                    lease_token=lease_token,
                    lease_seconds=DELIVERY_LEASE_SECONDS,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                log.exception("dataset import delivery claim failed")
                break
            if delivery is None:
                break

            totals["claimed"] += 1
            try:
                outcome = await _deliver_one(db, delivery, dispatcher)
            except Exception:
                await db.rollback()
                totals["deferred"] += 1
                log.exception(
                    "dataset import delivery deferred delivery_id=%s kind=%s target_id=%s",
                    delivery.delivery_id,
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
