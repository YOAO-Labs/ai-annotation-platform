"""Build immutable image training packages on the export queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import storage_service
from app.services import training_dataset_storage
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    build_training_package,
    instance_rle_payload,
    instance_rle_text,
)
from app.services.training_datasets import MAX_IMAGE_BYTES, assert_dataset_authority
from app.services.content_snapshot import ContentError
from app.services.reference_keys import is_reference
from training_contract.dataset import (
    is_instance_rle,
    instance_rle_budget,
    DatasetContractError,
)

from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_image(
    sample: dict,
    prefix: str,
    snapshot_digest: str,
    directory: Path,
    *,
    bucket: str,
    verified_reference=None,
) -> dict:
    """Reuse preserved source bytes on retries; never reread changed originals."""
    key = f"{prefix}/media/{sample['task_id']}"
    preserved = False
    try:
        head = storage_service.client.head_object(Bucket=bucket, Key=key)
        if head.get("Metadata", {}).get("snapshot-digest") != snapshot_digest:
            raise TrainingDatasetError(
                "preserved_media_conflict", "冻结图片的身份不匹配"
            )
        response = storage_service.client.get_object(
            Bucket=bucket, Key=key, IfMatch=head["ETag"]
        )
        preserved = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
            raise
        if is_reference(sample["source_key"]):
            if verified_reference is None:
                raise TrainingDatasetError(
                    "reference_read_required", "引用图片需要严格校验读取"
                )
            response = {"Body": verified_reference.file}
        else:
            params = {
                "Bucket": sample["source_bucket"],
                "Key": sample["source_key"],
                "IfMatch": sample["media"]["etag"],
            }
            if sample["media"].get("version_id"):
                params["VersionId"] = sample["media"]["version_id"]
            try:
                response = storage_service.client.get_object(**params)
            except ClientError as source_error:
                raise TrainingDatasetError(
                    "source_media_changed", "原始图片已变化或不可用，请创建新版本"
                ) from source_error
    path = directory / sample["task_id"]
    try:
        size = 0
        with path.open("wb") as target:
            for chunk in iter(lambda: response["Body"].read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise TrainingDatasetError(
                        "image_size_limit", "图片超过训练数据大小限制"
                    )
                target.write(chunk)
    finally:
        response["Body"].close()
    if size != sample["media"]["size_bytes"]:
        raise TrainingDatasetError("source_media_changed", "图片大小与冻结清单不一致")
    digest = file_digest(path)
    if preserved and head["Metadata"].get("sha256") != digest:
        raise TrainingDatasetError("preserved_media_corrupt", "冻结图片校验失败")
    if sample.get("asset_reference") and sample["asset_reference"]["sha256"] != digest:
        raise TrainingDatasetError(
            "source_media_changed", "图片摘要与固定 Asset 不一致"
        )
    try:
        with Image.open(path) as image:
            width, height = storage_service._logical_image_dimensions(image)
            extension = {
                "JPEG": ".jpg",
                "PNG": ".png",
                "WEBP": ".webp",
                "BMP": ".bmp",
                "TIFF": ".tif",
            }.get(image.format)
            if extension is None or width * height > 100_000_000:
                raise TrainingDatasetError(
                    "unsupported_image", "图片格式或像素数量不适合当前训练器"
                )
            if sample.get("raster_contract"):
                instance_rle_budget(width, height, len(sample["annotations"]))
            image.load()
    except (TrainingDatasetError, DatasetContractError):
        raise
    except Exception as exc:
        raise TrainingDatasetError("invalid_image", "图片无法完整解码") from exc
    if (sample.get("expected_width") and width != sample["expected_width"]) or (
        sample.get("expected_height") and height != sample["expected_height"]
    ):
        raise TrainingDatasetError(
            "image_dimensions_changed", "图片尺寸与冻结的标注坐标范围不一致"
        )
    if not preserved:
        storage_service.client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "Metadata": {"snapshot-digest": snapshot_digest, "sha256": digest}
            },
        )
    if sample.get("raster_contract"):
        # Independently retained training object, under the same version prefix
        # and deletion owner as the original frozen media.
        payload = instance_rle_payload(sample, sample["classes"], width, height)
        mask_path = directory / f"{sample['task_id']}.json"
        mask_path.write_text(instance_rle_text(payload))
        storage_service.upload_file(
            str(mask_path),
            f"{prefix}/masks/{sample['task_id']}.json",
            bucket=bucket,
            content_type="application/json",
        )
    return {
        **({"mask_path": str(mask_path)} if sample.get("raster_contract") else {}),
        "task_id": sample["task_id"],
        "path": str(path),
        "sha256": digest,
        "size_bytes": size,
        "width": width,
        "height": height,
        "extension": extension,
    }


async def materialize_sample(
    db,
    version,
    sample,
    prefix,
    snapshot_digest,
    directory,
    node_id,
):
    """Try retained bytes first, then resolve an authorized reference if needed."""
    owner = training_dataset_storage.resolve(version)
    if prefix != owner.prefix.rstrip("/"):
        raise TrainingDatasetError(
            "training_storage_invalid_key", "冻结前缀与版本归属不一致", status_code=409
        )
    owner.contains(f"{prefix}/media/{sample['task_id']}")
    try:
        return await asyncio.to_thread(
            materialize_image,
            sample,
            prefix,
            snapshot_digest,
            directory,
            bucket=owner.bucket,
        )
    except TrainingDatasetError as exc:
        if exc.code != "reference_read_required":
            raise
    if not node_id or not node_id.startswith("worker:"):
        raise TrainingDatasetError(
            "worker_read_not_verified", "训练构建节点尚未核验原图读取能力"
        )
    from app.db.models.user import User
    from app.services.training_dataset_sources import training_reference_snapshot

    await assert_dataset_authority(db, version)
    actor = await db.get(User, version.creator_id, populate_existing=True)
    content, _, _ = await training_reference_snapshot(
        db,
        sample,
        actor,
        api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
        node_id=node_id,
    )
    try:
        result = await asyncio.to_thread(
            materialize_image,
            sample,
            prefix,
            snapshot_digest,
            directory,
            bucket=owner.bucket,
            verified_reference=content,
        )
        await assert_dataset_authority(db, version)
        result["_source_backed"] = True
        return result
    finally:
        content.close()


async def build_dataset(
    version_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    session_factory,
    *,
    node_id: str | None = None,
) -> None:
    # Keep the connection checked out across domain transaction commits.
    async with session_factory() as session:
        bind = session.bind
        engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    async with engine.connect() as execution:
        key = func.hashtextextended(str(version_id), 1)
        acquired = await execution.scalar(select(func.pg_try_advisory_lock(key)))
        await execution.commit()
        if not acquired:
            return
        try:
            await _build_dataset(
                version_id, job_id, attempt_id, session_factory, node_id
            )
        finally:
            await execution.execute(select(func.pg_advisory_unlock(key)))
            await execution.commit()


async def _build_dataset(
    version_id, job_id, attempt_id, session_factory, node_id=None
) -> None:
    prefix = None
    owner = None
    published = False
    async with session_factory() as db:
        version = await db.get(TrainingDatasetVersion, version_id)
        if version is None:
            return
        try:
            await assert_dataset_authority(db, version)
            version = (
                await db.execute(
                    select(TrainingDatasetVersion)
                    .where(TrainingDatasetVersion.id == version_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if (
                version.async_job_id != job_id
                or version.attempt_id != attempt_id
                or version.status != "pending"
            ):
                return
            owner = training_dataset_storage.resolve(version)
            prefix = owner.prefix.rstrip("/")
            version.status = "running"
            await jobs.mark_running(db, job_id)
            snapshot = version.snapshot
            snapshot_digest = version.snapshot_digest
            parent = (
                await db.get(TrainingDatasetVersion, version.parent_id)
                if version.parent_id
                else None
            )
            parent_manifest = parent.manifest if parent else None
            await db.commit()
            with tempfile.TemporaryDirectory(prefix="aap-training-") as temporary:
                directory = Path(temporary)
                media = []
                source_reads = []
                for index, sample in enumerate(snapshot["samples"]):
                    media.append(
                        await materialize_sample(
                            db,
                            version,
                            {
                                **sample,
                                "raster_contract": True,
                                "classes": snapshot["classes"],
                            }
                            if is_instance_rle(snapshot)
                            else sample,
                            prefix,
                            snapshot_digest,
                            directory,
                            node_id,
                        )
                    )
                    if media[-1].pop("_source_backed", False):
                        source_reads.append(sample)
                    version = (
                        await db.execute(
                            select(TrainingDatasetVersion)
                            .where(TrainingDatasetVersion.id == version_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    ).scalar_one_or_none()
                    if (
                        version is None
                        or version.attempt_id != attempt_id
                        or version.async_job_id != job_id
                        or version.status != "running"
                    ):
                        return
                    version.progress_pct = int(
                        80 * (index + 1) / len(snapshot["samples"])
                    )
                    await jobs.update_progress(db, job_id, version.progress_pct)
                    await db.commit()
                package = directory / "dataset.zip"
                manifest = await asyncio.to_thread(
                    build_training_package,
                    snapshot,
                    media,
                    package,
                    parent_manifest=parent_manifest,
                )
                owner = training_dataset_storage.resolve(version)
                package_key = owner.key(f"attempts/{attempt_id}/dataset.zip")
                await asyncio.to_thread(
                    storage_service.upload_file,
                    str(package),
                    package_key,
                    bucket=owner.bucket,
                    content_type="application/zip",
                )
                package_digest = await asyncio.to_thread(file_digest, package)
                package_size = package.stat().st_size
                await assert_dataset_authority(db, version)
                version = (
                    await db.execute(
                        select(TrainingDatasetVersion)
                        .where(TrainingDatasetVersion.id == version_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if (
                    version.async_job_id != job_id
                    or version.attempt_id != attempt_id
                    or version.status != "running"
                ):
                    return
                if source_reads:
                    from app.services.training_dataset_sources import (
                        lock_source_maintenance,
                    )

                    await lock_source_maintenance(db, {"samples": source_reads})
                version.manifest = manifest
                version.package_key = package_key
                version.package_sha256 = package_digest
                version.size_bytes = package_size
                version.status = "ready"
                version.progress_pct = 100
                version.completed_at = datetime.now(timezone.utc)
                await jobs.mark_complete(
                    db,
                    job_id,
                    result={
                        "training_dataset_id": str(version_id),
                        "summary": manifest["summary"],
                    },
                )
                await notify_job_terminal(db, job_id=job_id)
                await db.commit()
                published = True
        except Exception as exc:
            await db.rollback()
            version = (
                await db.execute(
                    select(TrainingDatasetVersion)
                    .where(TrainingDatasetVersion.id == version_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                version is not None
                and version.attempt_id == attempt_id
                and version.async_job_id == job_id
                and version.status in {"pending", "running"}
            ):
                version.status = "failed"
                version.error_code = (
                    exc.code
                    if isinstance(
                        exc, (TrainingDatasetError, DatasetContractError, ContentError)
                    )
                    else "build_failed"
                )
                version.error_message = (
                    str(exc)
                    if isinstance(
                        exc, (TrainingDatasetError, DatasetContractError, ContentError)
                    )
                    else "训练数据构建失败，请检查素材或重试"
                )
                version.completed_at = datetime.now(timezone.utc)
                await jobs.mark_failed(db, job_id, error=version.error_message)
                await notify_job_terminal(db, job_id=job_id)
                await db.commit()
        finally:
            await db.rollback()
            if owner is not None and prefix and not published:
                cleanup = False
                try:
                    # A commit acknowledgement can fail after publication succeeded.
                    referenced_key = await db.scalar(
                        select(TrainingDatasetVersion.package_key).where(
                            TrainingDatasetVersion.id == version_id
                        )
                    )
                    cleanup = (
                        referenced_key != f"{prefix}/attempts/{attempt_id}/dataset.zip"
                    )
                except Exception:
                    log.exception(
                        "Cannot verify training attempt cleanup: %s", attempt_id
                    )
                finally:
                    await db.rollback()
                try:
                    if cleanup:
                        owner = training_dataset_storage.resolve_ref(owner.ref)
                        await asyncio.to_thread(
                            storage_service.delete_prefix,
                            f"{prefix}/attempts/{attempt_id}/",
                            bucket=owner.bucket,
                        )
                except Exception:
                    log.exception("Training attempt cleanup failed: %s", attempt_id)


@celery_app.task(name="app.workers.training_dataset.run_training_dataset", bind=True)
def run_training_dataset(self, version_id: str, job_id: str, attempt_id: str) -> None:
    async def run():
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            await build_dataset(
                uuid.UUID(version_id),
                uuid.UUID(job_id),
                uuid.UUID(attempt_id),
                async_sessionmaker(engine, expire_on_commit=False),
                node_id=f"worker:{self.request.hostname}",
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
