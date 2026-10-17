"""Durable storage cleanup staged with business deletion before IO."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services.storage import storage_service as storage

log = logging.getLogger(__name__)


def stage_deletion(db, prefix):
    # No FK from the tombstone: project removal must not cancel pending cleanup.
    intent = TrainingStorageDeletion(bucket=storage.training_bucket, prefix=prefix)
    db.add(intent)
    return intent


async def purge_committed(db, intent_id):
    try:
        await purge_requested(db, intent_id)
    except Exception:
        # Business deletion is already committed. Return success while the
        # existing maintenance owner retries the independent storage intent.
        log.warning("Training storage deletion deferred intent=%s", intent_id)


async def purge_requested(db, intent_id):
    intent = await db.scalar(
        select(TrainingStorageDeletion)
        .where(TrainingStorageDeletion.id == intent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if intent is None:
        return False
    prefix, bucket = intent.prefix, intent.bucket
    intent.last_attempted_at = datetime.now(timezone.utc)
    await db.commit()
    # Repeated prefix deletion is safe after crashes or an acknowledgement
    # commit failure. Keep the durable intent until both operations succeed.
    await asyncio.to_thread(storage.delete_prefix, prefix, bucket=bucket)
    intent = await db.scalar(
        select(TrainingStorageDeletion)
        .where(TrainingStorageDeletion.id == intent_id)
        .with_for_update()
    )
    if intent is not None:
        await db.delete(intent)
        await db.commit()
    return True


async def maintain(factory):
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(TrainingStorageDeletion.id)
                    .order_by(
                        TrainingStorageDeletion.last_attempted_at.asc().nulls_first(),
                        TrainingStorageDeletion.created_at,
                        TrainingStorageDeletion.id,
                    )
                    .limit(100)
                )
            ).all()
        )
    for intent_id in ids:
        async with factory() as db:
            try:
                await purge_requested(db, intent_id)
            except Exception:
                log.warning("Training storage deletion deferred intent=%s", intent_id)
