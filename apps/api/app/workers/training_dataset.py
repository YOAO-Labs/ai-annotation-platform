"""Build immutable image training packages on the export queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError
from fastapi import HTTPException
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
from app.services import training_dataset_storage, training_storage_io
from app.services import training_dataset_originals as originals
from app.services import storage_access
from app.services.training_dataset_bridge import content_digest
from app.db.models.async_job import AsyncJob
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


class Superseded(Exception):
    pass


@dataclass(frozen=True)
class Execution:
    version_id: uuid.UUID
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    snapshot_digest: str
    owner_ref: dict
    authority: originals.Authority
    celery_task_id: str | None


async def guard_execution(db, context, *, lock=False, status="running"):
    statement = select(TrainingDatasetVersion).where(
        TrainingDatasetVersion.id == context.version_id
    )
    if lock:
        statement = statement.with_for_update()
    version = await db.scalar(statement.execution_options(populate_existing=True))
    job = (
        await jobs.lock_job(db, context.job_id)
        if lock
        else await db.get(AsyncJob, context.job_id, populate_existing=True)
    )
    if (version and version.status == "cancelled") or (
        job
        and (job.status == "cancelled" or (job.payload or {}).get("cancel_requested"))
    ):
        raise asyncio.CancelledError
    if (
        version is None
        or job is None
        or version.status != status
        or job.status != status
        or version.attempt_id != context.attempt_id
        or version.async_job_id != context.job_id
        or job.celery_task_id != context.celery_task_id
    ):
        raise Superseded
    if (
        version.project_id != context.authority.project_id
        or version.creator_id != context.authority.actor_id
        or job.project_id != version.project_id
        or job.user_id != version.creator_id
        or job.kind != "training_dataset"
        or (job.payload or {}).get("training_dataset_id") != str(version.id)
        or version.snapshot_digest != context.snapshot_digest
        or content_digest(version.snapshot) != context.snapshot_digest
        or training_dataset_storage.resolve(version).ref != context.owner_ref
    ):
        raise ContentError("training_input_binding_changed")
    return version


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with storage_access.closing_registered(path.open("rb")) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_output(
    bucket, key, expected_sha256, expected_size, *, snapshot_digest=None, transport=None
):
    """Read back the actual saved object, without trusting its SHA metadata."""
    destination = transport or storage_service
    head = destination.client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != expected_size or (
        snapshot_digest is not None
        and head.get("Metadata", {}).get("snapshot-digest") != snapshot_digest
    ):
        raise TrainingDatasetError(
            "training_output_mismatch", "训练文件写回大小或归属校验失败"
        )
    params = {"Bucket": bucket, "Key": key, "IfMatch": head["ETag"]}
    if head.get("VersionId"):
        params["VersionId"] = head["VersionId"]
    response = destination.client.get_object(**params)
    digest, size = hashlib.sha256(), 0
    with storage_access.closing_registered(response["Body"]) as body:
        while chunk := body.read(min(1024 * 1024, expected_size - size + 1)):
            size += len(chunk)
            if size > expected_size:
                raise TrainingDatasetError(
                    "training_output_mismatch", "训练文件写回内容校验失败"
                )
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise TrainingDatasetError(
            "training_output_mismatch", "训练文件写回内容校验失败"
        )


def materialize_image(
    sample: dict,
    prefix: str,
    snapshot_digest: str,
    directory: Path,
    *,
    bucket: str,
    verified_reference=None,
    verified_original=None,
    transport=None,
) -> dict:
    """Reuse preserved source bytes on retries; never reread changed originals."""
    if transport is None and training_dataset_storage.MANAGED_NAMESPACE in prefix + "/":
        training_dataset_storage._reject("training_storage_binding_unproven")
    destination = transport or storage_service
    key = f"{prefix}/media/{sample['task_id']}"
    preserved = False
    try:
        head = destination.client.head_object(Bucket=bucket, Key=key)
        if head.get("Metadata", {}).get("snapshot-digest") != snapshot_digest:
            raise TrainingDatasetError(
                "preserved_media_conflict", "冻结图片的身份不匹配"
            )
        response = destination.client.get_object(
            Bucket=bucket, Key=key, IfMatch=head["ETag"]
        )
        preserved = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
            raise
        if verified_original is not None:
            originals.validate_receipt(sample.get("original"))
            verified_original.file.seek(0)
            response = {"Body": verified_original.file}
        elif is_reference(sample["source_key"]):
            if verified_reference is None:
                raise TrainingDatasetError(
                    "reference_read_required", "引用图片需要严格校验读取"
                )
            verified_reference.file.seek(0)
            response = {"Body": verified_reference.file}
        elif sample.get("original") is not None:
            originals.validate_receipt(sample["original"])
            raise TrainingDatasetError(
                "original_read_required", "托管图片需要已登记原件校验读取"
            )
        else:
            raise TrainingDatasetError(
                "original_input_unrecorded",
                "原图缺少内容身份且没有保留副本，请创建新版本",
            )
    path = directory / sample["task_id"]
    try:
        size = 0
        with storage_access.closing_registered(path.open("wb")) as target:
            for chunk in iter(lambda: response["Body"].read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise TrainingDatasetError(
                        "image_size_limit", "图片超过训练数据大小限制"
                    )
                target.write(chunk)
    finally:
        if preserved:
            storage_access.close_registered(response["Body"])
    if size != sample["media"]["size_bytes"]:
        raise TrainingDatasetError("source_media_changed", "图片大小与冻结清单不一致")
    digest = file_digest(path)
    if preserved and head["Metadata"].get("sha256") != digest:
        raise TrainingDatasetError("preserved_media_corrupt", "冻结图片校验失败")
    if sample.get("original") is not None:
        expected = originals.validate_receipt(sample["original"])
        if digest != expected["sha256"] or size != expected["size_bytes"]:
            raise TrainingDatasetError(
                "source_media_changed", "图片内容与固定原件身份不一致"
            )
    if sample.get("asset_reference") and sample["asset_reference"]["sha256"] != digest:
        raise TrainingDatasetError(
            "source_media_changed", "图片摘要与固定 Asset 不一致"
        )
    try:
        with storage_access.closing_registered(Image.open(path)) as image:
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
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        raise TrainingDatasetError("invalid_image", "图片无法完整解码") from exc
    if (sample.get("expected_width") and width != sample["expected_width"]) or (
        sample.get("expected_height") and height != sample["expected_height"]
    ):
        raise TrainingDatasetError(
            "image_dimensions_changed", "图片尺寸与冻结的标注坐标范围不一致"
        )
    if not preserved:
        destination.client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "Metadata": {"snapshot-digest": snapshot_digest, "sha256": digest}
            },
        )
        verify_output(
            bucket,
            key,
            digest,
            size,
            snapshot_digest=snapshot_digest,
            transport=destination,
        )
    if sample.get("raster_contract"):
        # Independently retained training object, under the same version prefix
        # and deletion owner as the original frozen media.
        payload = instance_rle_payload(sample, sample["classes"], width, height)
        mask_path = directory / f"{sample['task_id']}.json"
        with storage_access.closing_registered(mask_path.open("w")) as target:
            target.write(instance_rle_text(payload))
        destination.upload_file(
            str(mask_path),
            f"{prefix}/masks/{sample['task_id']}.json",
            bucket=bucket,
            content_type="application/json",
        )
        verify_output(
            bucket,
            f"{prefix}/masks/{sample['task_id']}.json",
            file_digest(mask_path),
            mask_path.stat().st_size,
            transport=destination,
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


def _materialize_image(target, *args, **kwargs):
    if target is None:
        return materialize_image(*args, **kwargs)
    return training_storage_io.physical(
        target, lambda bound: materialize_image(*args, transport=bound, **kwargs)
    )


async def materialize_sample(
    db,
    version,
    sample,
    prefix,
    snapshot_digest,
    directory,
    node_id,
    *,
    authority=None,
    guard=None,
    target=None,
):
    """Try retained bytes first, then resolve an authorized reference if needed."""
    owner = training_dataset_storage.resolve(version)
    if prefix != owner.prefix.rstrip("/"):
        raise TrainingDatasetError(
            "training_storage_invalid_key", "冻结前缀与版本归属不一致", status_code=409
        )
    owner.contains(f"{prefix}/media/{sample['task_id']}")
    if target is None and owner.key_prefix:
        target = await training_storage_io.prepare(db, owner)

    async def checkpoint():
        if authority is not None:
            await originals.check(db, authority)
        if guard is not None:
            await guard()
        if authority is not None or guard is not None:
            await db.commit()
        if authority is not None:
            originals.clock(authority)

    await checkpoint()
    try:
        result = await originals.physical(
            _materialize_image,
            target,
            sample,
            prefix,
            snapshot_digest,
            directory,
            bucket=owner.bucket,
            timeout=120,
        )
        await checkpoint()
        return result
    except TrainingDatasetError as exc:
        if exc.code not in {"reference_read_required", "original_read_required"}:
            raise
        managed = exc.code == "original_read_required"
    if managed:
        contract = version.snapshot.get("managed_original_contract")
        if type(contract) is not int or contract != 1:
            raise ContentError("original_input_unrecorded")
        authority = authority or await originals.delegated(db, version)
        async with originals.media(db, authority, sample, guard=guard) as (
            stage,
            source,
        ):
            result = await originals.physical(
                _materialize_image,
                target,
                sample,
                prefix,
                snapshot_digest,
                directory,
                bucket=owner.bucket,
                verified_original=stage,
                timeout=120,
            )
        result["_managed_source"] = source
        return result
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
    failure = None
    try:
        result = await originals.physical(
            _materialize_image,
            target,
            sample,
            prefix,
            snapshot_digest,
            directory,
            bucket=owner.bucket,
            verified_reference=content,
            timeout=120,
        )
        await assert_dataset_authority(db, version)
        await checkpoint()
        result["_source_backed"] = True
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await originals.physical(content.close)
        except BaseException as closing:
            if getattr(failure, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(closing)
            raise


async def build_dataset(
    version_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    session_factory,
    *,
    node_id: str | None = None,
    celery_task_id: str | None = None,
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
                version_id, job_id, attempt_id, session_factory, node_id, celery_task_id
            )
        finally:

            async def release():
                await execution.execute(select(func.pg_advisory_unlock(key)))
                await execution.commit()

            await storage_access.finish(asyncio.create_task(release()))


async def _build_dataset(
    version_id, job_id, attempt_id, session_factory, node_id=None, celery_task_id=None
) -> None:
    prefix = None
    owner = None
    published = False
    unsettled = False
    permit = None
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
            authority = await originals.delegated(db, version)
            job = await jobs.lock_job(db, job_id)
            context = Execution(
                version_id,
                job_id,
                attempt_id,
                version.snapshot_digest,
                owner.ref,
                authority,
                job.celery_task_id if job else None,
            )
            if celery_task_id is not None and celery_task_id != context.celery_task_id:
                return
            await guard_execution(db, context, lock=True, status="pending")
            from app.db.models.storage_maintenance import StorageOperation

            if await db.scalar(
                select(StorageOperation.id)
                .where(StorageOperation.purpose == originals.output_purpose(version_id))
                .limit(1)
            ):
                raise storage_access.mark_unsettled(ContentError("execution_stopping"))
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
            target = await training_storage_io.prepare(db, owner)
            permit = await storage_access.acquire(
                db,
                uuid.UUID(owner.ref["location_id"]),
                originals.output_purpose(version_id),
            )
            async with permit:
                with originals.temporary_directory(prefix="aap-training-") as temporary:
                    directory = Path(temporary)
                    media = []
                    source_reads = []
                    managed_reads = {}

                    async def guard():
                        return await guard_execution(db, context)

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
                                authority=authority,
                                guard=guard,
                                target=target,
                            )
                        )
                        if media[-1].pop("_source_backed", False):
                            source_reads.append(sample)
                        source = media[-1].pop("_managed_source", None)
                        if source is not None:
                            managed_reads[sample["task_id"]] = source
                        version = await guard_execution(db, context, lock=True)
                        version.progress_pct = int(
                            80 * (index + 1) / len(snapshot["samples"])
                        )
                        await jobs.update_progress(db, job_id, version.progress_pct)
                        await db.commit()
                    package = directory / "dataset.zip"
                    manifest = await originals.physical(
                        build_training_package,
                        snapshot,
                        media,
                        package,
                        parent_manifest=parent_manifest,
                    )
                    owner = training_dataset_storage.resolve(version)
                    package_key = owner.key(f"attempts/{attempt_id}/dataset.zip")
                    await originals.physical(
                        training_storage_io.physical,
                        target,
                        lambda bound: bound.upload_file(
                            str(package),
                            package_key,
                            bucket=owner.bucket,
                            content_type="application/zip",
                        ),
                    )
                    package_digest = await originals.physical(file_digest, package)
                    package_size = package.stat().st_size
                    await originals.physical(
                        training_storage_io.physical,
                        target,
                        lambda bound: verify_output(
                            owner.bucket,
                            package_key,
                            package_digest,
                            package_size,
                            transport=bound,
                        ),
                    )
                await assert_dataset_authority(db, version)
                await originals.check(db, authority)
                version = await guard_execution(db, context, lock=True)
                await storage_access.check(
                    db,
                    uuid.UUID(owner.ref["location_id"]),
                    expected_revision=permit.revision,
                    lock=True,
                )
                for source in managed_reads.values():
                    await originals.recheck(db, authority, source)
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
                originals.clock(authority)
                await db.commit()
                published = True
        except Superseded:
            await db.rollback()
        except (Exception, asyncio.CancelledError) as exc:
            unsettled = getattr(exc, "storage_operation_unsettled", False)
            if isinstance(exc, HTTPException) and exc.status_code in {401, 403}:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                exc = TrainingDatasetError(
                    "api_key_revoked"
                    if detail.get("code") == "api_key_authority_revoked"
                    else "authority_revoked",
                    "训练数据版本的凭证或权限已失效",
                    status_code=403,
                )
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
                cancelled = isinstance(exc, asyncio.CancelledError)
                version.status = "cancelled" if cancelled else "failed"
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
                if cancelled:
                    version.error_code = version.error_message = None
                    await jobs.mark_cancelled(db, job_id)
                else:
                    await jobs.mark_failed(db, job_id, error=version.error_message)
                await notify_job_terminal(db, job_id=job_id)
                await db.commit()
        finally:
            await db.rollback()
            if owner is not None and prefix and not published and not unsettled:
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
                        cleanup_target = await training_storage_io.prepare(db, owner)
                        cleanup_permit = await storage_access.acquire(
                            db,
                            uuid.UUID(owner.ref["location_id"]),
                            originals.output_purpose(version_id),
                            expected_revision=permit.revision if permit else None,
                        )
                        async with cleanup_permit:
                            await originals.physical(
                                training_storage_io.physical,
                                cleanup_target,
                                lambda bound: bound.delete_prefix(
                                    f"{prefix}/attempts/{attempt_id}/",
                                    bucket=owner.bucket,
                                ),
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
                celery_task_id=self.request.id,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
