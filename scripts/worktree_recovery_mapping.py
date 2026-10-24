"""Explicit physical changes in an offline, owned recovery target only.

Immutable snapshots, manifests, business IDs and historical observations are
deliberately outside this whitelist. This module never starts restored jobs.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from worktree_env import BUCKETS, WorktreeError


def bucket_mapping(source: dict, target: dict) -> dict[str, str]:
    if set(source) != set(BUCKETS) or set(target) != set(BUCKETS):
        raise WorktreeError("恢复映射必须包含全部九用途")
    if any(
        not isinstance(value, str) or not value
        for value in (*source.values(), *target.values())
    ):
        raise WorktreeError("恢复桶名必须为非空字符串")
    pairs = {}
    for purpose in BUCKETS:
        before, after = source[purpose], target[purpose]
        if before in pairs and pairs[before] != after:
            raise WorktreeError("恢复不能拆分共用桶")
        pairs[before] = after
    if len(set(pairs.values())) != len(pairs):
        raise WorktreeError("恢复不能合并独立桶")
    if set(source.values()) & set(target.values()):
        raise WorktreeError("恢复目标必须与源桶完全隔离")
    return pairs


def identity(endpoint_url: str, bucket: str) -> str:
    return hashlib.sha256(
        json.dumps([endpoint_url.rstrip("/"), bucket]).encode()
    ).hexdigest()


async def remap_storage(
    db, source: dict, target: dict, endpoint_url: str
) -> list[dict]:
    """Validate every supported row before modifying this caller's transaction."""
    from sqlalchemy import select, text
    from app.services.dataset_assets import (
        LEGACY_LOCATION_ID,
        PURPOSE_BUCKET_ATTRIBUTES,
    )
    from app.db.models.dataset_asset import (
        DatasetAssetLocation,
        StorageBinding,
        StorageLocation,
    )
    from app.db.models.dataset_source import DatasetSourcePreview
    from app.db.models.original_cache import OriginalCacheEntry
    from app.db.models.managed_original_retirement import ManagedOriginalRetirement
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.training_storage_deletion import TrainingStorageDeletion
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingRun
    from app.db.models.training_preview import TrainingPreview
    from app.db.models.sample_evaluation import SampleEvaluationRun
    from app.db.models.active_learning import ActiveLearningRound
    from app.services import (
        training_dataset_storage,
        training_run_storage,
        training_preview_storage,
        sample_evaluation_storage,
        active_learning_storage,
    )

    mapping = bucket_mapping(source, target)
    planned = []
    ledger = []

    def change(row, field, value, table, key):
        before = getattr(row, field)
        if before != value:
            planned.append((row, field, value))
            ledger.append(
                {
                    "table": table,
                    "id": key,
                    "field": field,
                    "before": before,
                    "after": value,
                }
            )

    def remap(row, table, key):
        if row.bucket not in mapping:
            raise WorktreeError(f"{table} 存在不属于备份的物理桶")
        change(row, "bucket", mapping[row.bucket], table, key)

    location = await db.get(StorageLocation, LEGACY_LOCATION_ID)
    scheme, endpoint = endpoint_url.split("://", 1)
    if (
        location is None
        or location.kind != "s3"
        or location.config_revision != 1
        or location.config
        != {"endpoint": endpoint.rstrip("/"), "use_ssl": scheme == "https"}
    ):
        raise WorktreeError("恢复平台位置与备份端点不一致")
    bindings = list(await db.scalars(select(StorageBinding)))
    if {row.purpose for row in bindings} != set(PURPOSE_BUCKET_ATTRIBUTES):
        raise WorktreeError("恢复平台绑定缺失或含未知用途")
    by_suffix = {value: name for name, value in BUCKETS.items()}
    for row in bindings:
        role = by_suffix[row.purpose]
        if (
            row.location_id != LEGACY_LOCATION_ID
            or row.generation != 1
            or row.bucket != source[role]
        ):
            raise WorktreeError("恢复平台绑定与源配置不一致")
        remap(row, "storage_bindings", row.purpose)
    for row in await db.scalars(select(DatasetAssetLocation)):
        if row.location_id == LEGACY_LOCATION_ID:
            remap(row, "dataset_asset_locations", f"{row.asset_id}:{row.location_id}")
        elif row.bucket in mapping:
            raise WorktreeError("外部位置意外指向平台桶，需要独立恢复合同")
    for row in await db.scalars(select(DatasetSourcePreview)):
        if row.bucket is not None:
            if row.location_id != LEGACY_LOCATION_ID:
                raise WorktreeError("预览存在未知物理位置")
            remap(row, "dataset_source_previews", f"{row.source_id}:{row.path_digest}")
    for model, role in (
        (OriginalCacheEntry, "MINIO_MEDIA_CACHE_BUCKET"),
        (PredictionInputLease, "MINIO_IMPORT_BUCKET"),
    ):
        for row in await db.scalars(select(model)):
            table = model.__tablename__
            if row.bucket != source[role] or row.storage_identity != identity(
                endpoint_url, source[role]
            ):
                raise WorktreeError(f"{table} 存储身份与源配置不一致")
            if model is OriginalCacheEntry and (
                row.location_id != LEGACY_LOCATION_ID or row.binding_generation != 1
            ):
                raise WorktreeError("完整缓存存在未知绑定")
            remap(row, table, str(row.id))
            change(
                row,
                "storage_identity",
                identity(endpoint_url, target[role]),
                table,
                str(row.id),
            )
    owner_types = {
        TrainingDatasetVersion: training_dataset_storage,
        TrainingRun: training_run_storage,
        TrainingPreview: training_preview_storage,
        SampleEvaluationRun: sample_evaluation_storage,
        ActiveLearningRound: active_learning_storage,
    }
    for model in (*owner_types, TrainingStorageDeletion):
        for row in await db.scalars(select(model)):
            ref = row.storage_ref
            if ref is not None:
                owners = owner_types.get(model)
                if model is TrainingStorageDeletion:
                    owners = {
                        "training-dataset": training_dataset_storage,
                        "training-run": training_run_storage,
                    }.get(ref.get("purpose") if isinstance(ref, dict) else None)
                    if owners is None:
                        raise WorktreeError("恢复清理意图含未知归属")
                expected = (
                    row.prefix
                    if model is TrainingStorageDeletion
                    else owners.prefix_for(row.project_id, row.id)
                )
                try:
                    owner = owners.resolve_ref(ref, expected_prefix=expected)
                except ValueError:
                    raise WorktreeError("训练对象或清理意图含未知归属") from None
                if (
                    ref["endpoint_url"] != endpoint_url.rstrip("/")
                    or owner.bucket != source["MINIO_TRAINING_BUCKET"]
                    or (model is TrainingStorageDeletion and row.bucket != owner.bucket)
                ):
                    raise WorktreeError("训练版本归属与备份源不一致")
                bucket = target["MINIO_TRAINING_BUCKET"]
                change(
                    row,
                    "storage_ref",
                    {
                        **ref,
                        "bucket": bucket,
                        "storage_identity": identity(endpoint_url, bucket),
                    },
                    model.__tablename__,
                    str(row.id),
                )
            if model is TrainingStorageDeletion:
                remap(row, "training_storage_deletions", str(row.id))
    for row in await db.scalars(select(ManagedOriginalRetirement)):
        if (
            row.location_id != LEGACY_LOCATION_ID
            or row.location_config_revision != 1
            or row.binding_generation != 1
            or row.identity_state != "verified_binding"
            or row.role != "managed_original"
            or row.bucket != source["MINIO_DATASETS_BUCKET"]
            or row.storage_identity
            != identity(endpoint_url, source["MINIO_DATASETS_BUCKET"])
        ):
            raise WorktreeError("托管原件保留记录存在未知物理身份，需要独立恢复合同")
        table = "managed_original_retirements"
        remap(row, table, str(row.id))
        change(
            row,
            "storage_identity",
            identity(endpoint_url, target["MINIO_DATASETS_BUCKET"]),
            table,
            str(row.id),
        )
    # Only the table owner may opt into the narrowly scoped JSON physical map.
    # Any failure rolls back the caller's complete offline mapping transaction.
    await db.execute(text("SET LOCAL aap.restore_training_storage = 'on'"))
    for row, field, value in planned:
        setattr(row, field, value)
    await db.flush()
    await db.execute(text("SET LOCAL aap.restore_training_storage = 'off'"))
    return ledger


def storage_view(endpoint_url: str, buckets: dict):
    """Build only the non-secret fields used by existing identity helpers."""
    return SimpleNamespace(
        client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint_url)),
        media_cache_bucket=buckets["MINIO_MEDIA_CACHE_BUCKET"],
        import_bucket=buckets["MINIO_IMPORT_BUCKET"],
    )
