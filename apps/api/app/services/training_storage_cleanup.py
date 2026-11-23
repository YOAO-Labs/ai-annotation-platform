"""Durable storage cleanup staged with business deletion before IO."""

import logging
from datetime import datetime, timezone

from uuid import uuid4

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from app.services import training_storage_io as storage_io
from app.services.training_dataset_bridge import TrainingDatasetError

log = logging.getLogger(__name__)


async def stage_deletion(db, record, prefix):
    """Copy the persisted owner into the caller's uncommitted deletion intent."""
    from app.services import training_dataset_storage, training_run_storage

    dataset = prefix == training_dataset_storage.prefix_for(
        record.project_id, record.id
    )
    purpose = "training-dataset" if dataset else "training-run"
    resolver = training_dataset_storage if dataset else training_run_storage
    owner = resolver.resolve(record)
    if prefix != owner.logical_prefix:
        raise TrainingDatasetError(
            "training_storage_invalid_key",
            "删除前缀与固定版本不一致",
            status_code=409,
        )
    intent_id = await db.scalar(
        select(
            func.yoao_learning_v1.training_storage_stage_deletion(
                uuid4(), record.id, purpose, literal(owner.ref, JSONB)
            )
        )
    )
    if intent_id is None:
        raise TrainingDatasetError(
            "training_storage_identity_changed",
            "清理对象的存储归属已变化",
            status_code=409,
        )
    return intent_id


async def purge_committed(db, intent_id):
    try:
        await purge_requested(db, intent_id)
    except Exception:
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
    source = func.yoao_learning_v1.training_storage_claim_deletion(
        intent_id, datetime.now(timezone.utc)
    ).table_valued("bucket", "prefix", "storage_ref")
    intent = (await db.execute(select(source))).one_or_none()
    if intent is None:
        return False
    prefix, bucket = intent.prefix, intent.bucket
    await db.commit()
    owner = _pinned_owner(intent)
    await storage_io.run(
        db,
        owner,
        "training_cleanup",
        lambda transport: transport.delete_prefix(prefix, bucket=bucket),
    )
    await db.execute(
        select(
            func.yoao_learning_v1.training_storage_complete_deletion(
                intent_id, literal(intent.storage_ref, JSONB)
            )
        )
    )
    await db.commit()
    return True


async def maintain(factory):
    async with factory() as db:
        batch = func.yoao_learning_v1.training_storage_deletion_batch().table_valued(
            "id"
        )
        ids = list(await db.scalars(select(batch.c.id)))
    for intent_id in ids:
        async with factory() as db:
            try:
                await purge_requested(db, intent_id)
            except Exception:
                log.warning("Training storage deletion deferred intent=%s", intent_id)
