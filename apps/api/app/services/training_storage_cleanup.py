"""Commit training deletion before IO; retry through evaluation maintenance."""

from app.services import training_storage_io as storage_io
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services.training_dataset_bridge import TrainingDatasetError

_UNRESOLVED_PREFIX = re.compile(
    r"projects/[0-9a-f-]{36}/(?:datasets|runs)/[0-9a-f-]{36}/"
)

log = logging.getLogger(__name__)


async def delete_record(db, record, prefix):
    # The database checks every remaining FK before storage can be destroyed.
    # No FK from the tombstone: project removal must not cancel pending cleanup.
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingRun
    from app.services import training_dataset_storage, training_run_storage

    if isinstance(record, (TrainingDatasetVersion, TrainingRun)):
        owner = (
            training_dataset_storage
            if isinstance(record, TrainingDatasetVersion)
            else training_run_storage
        ).resolve(record)
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
        raise TrainingDatasetError(
            "training_storage_identity_changed",
            "无法核实清理对象的存储归属",
            status_code=409,
        )
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
        from app.services import training_dataset_storage, training_run_storage

        resolver = (
            training_run_storage
            if isinstance(intent.storage_ref, dict)
            and intent.storage_ref.get("purpose") == "training-run"
            else training_dataset_storage
        )
        owner = resolver.resolve_ref(intent.storage_ref, expected_prefix=prefix)
        if owner.bucket != bucket:
            raise TrainingDatasetError(
                "training_storage_identity_changed",
                "删除意图的存储身份不一致",
                status_code=409,
            )
    elif _UNRESOLVED_PREFIX.fullmatch(prefix):
        raise TrainingDatasetError(
            "training_storage_unpinned",
            "旧训练删除意图的端点尚未核实，未执行物理清理",
            status_code=409,
        )
    # Repeated prefix deletion is safe after crashes or an acknowledgement
    # commit failure. Keep the durable intent until both operations succeed.
    if intent.storage_ref is None:
        raise TrainingDatasetError(
            "training_storage_unpinned",
            "旧训练删除意图的端点尚未核实，未执行物理清理",
            status_code=409,
        )
    await storage_io.run(
        db,
        owner,
        "training_cleanup",
        lambda transport: transport.delete_prefix(prefix, bucket=bucket),
    )
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
