"""Durable storage cleanup staged with business deletion before IO."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import training_storage_io as storage_io
from app.services.training_dataset_bridge import TrainingDatasetError

log = logging.getLogger(__name__)


def stage_deletion(db, record, prefix):
    """Stage one pinned storage intent; the caller owns deletion and commit."""
    # No FK from the tombstone: project removal must not cancel pending cleanup.
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingRun
    from app.services import training_dataset_storage, training_run_storage

    if not isinstance(record, (TrainingDatasetVersion, TrainingRun)):
        raise TrainingDatasetError(
            "training_storage_identity_changed",
            "无法核实清理对象的存储归属",
            status_code=409,
        )
    owner = (
        training_dataset_storage
        if isinstance(record, TrainingDatasetVersion)
        else training_run_storage
    ).resolve(record)
    if prefix != owner.logical_prefix:
        raise TrainingDatasetError(
            "training_storage_invalid_key",
            "删除前缀与固定版本不一致",
            status_code=409,
        )
    intent = TrainingStorageDeletion(
        bucket=owner.bucket, prefix=owner.prefix, storage_ref=owner.ref
    )
    db.add(intent)
    return intent


async def purge_committed(db, intent_id):
    try:
        await purge_requested(db, intent_id)
    except Exception:
        # Business deletion is already committed. Return success while the
        # existing maintenance owner retries the independent storage intent.
        log.warning("Training storage deletion deferred intent=%s", intent_id)


def _pinned_owner(intent):
    """Resolve the saved owner of one pinned cleanup intent."""
    if intent.storage_ref is None:
        raise TrainingDatasetError(
            "training_storage_unpinned",
            "旧训练删除意图缺少固定存储端点，未执行物理清理",
            status_code=409,
        )
    from app.services import training_dataset_storage, training_run_storage

    resolver = (
        training_run_storage
        if isinstance(intent.storage_ref, dict)
        and intent.storage_ref.get("purpose") == "training-run"
        else training_dataset_storage
    )
    owner = resolver.resolve_ref(intent.storage_ref, expected_prefix=intent.prefix)
    if owner.bucket != intent.bucket:
        raise TrainingDatasetError(
            "training_storage_identity_changed",
            "删除意图的存储身份不一致",
            status_code=409,
        )
    return owner


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
    owner = _pinned_owner(intent)
    # Repeated prefix deletion is safe after crashes or an acknowledgement
    # commit failure. Keep the durable intent until both operations succeed.
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
