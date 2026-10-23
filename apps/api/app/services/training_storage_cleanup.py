"""Commit training deletion before IO; retry through evaluation maintenance."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import TrainingDatasetError

_DATASET_PREFIX = re.compile(r"projects/[0-9a-f-]{36}/datasets/[0-9a-f-]{36}/")

log = logging.getLogger(__name__)


async def delete_record(db, record, prefix):
    # The database checks every remaining FK before storage can be destroyed.
    # No FK from the tombstone: project removal must not cancel pending cleanup.
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.services.training_dataset_storage import resolve

    if isinstance(record, TrainingDatasetVersion):
        owner = resolve(record)
        if prefix != owner.prefix:
            raise TrainingDatasetError(
                "training_storage_invalid_key",
                "删除前缀与固定版本不一致",
                status_code=409,
            )
        intent = TrainingStorageDeletion(
            bucket=owner.bucket, prefix=owner.prefix, storage_ref=owner.ref
        )
    else:
        # Run artifacts retain their separate cleanup contract.
        intent = TrainingStorageDeletion(bucket=storage.training_bucket, prefix=prefix)
    db.add(intent)
    await db.delete(record)
    await db.commit()
    intent_id = intent.id
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
    if intent.storage_ref is not None:
        from app.services.training_dataset_storage import resolve_ref

        owner = resolve_ref(intent.storage_ref, expected_prefix=prefix)
        if owner.bucket != bucket:
            raise TrainingDatasetError(
                "training_storage_identity_changed",
                "删除意图的存储身份不一致",
                status_code=409,
            )
    elif _DATASET_PREFIX.fullmatch(prefix):
        raise TrainingDatasetError(
            "training_storage_unpinned",
            "旧版本删除意图的端点尚未核实，未执行物理清理",
            status_code=409,
        )
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
