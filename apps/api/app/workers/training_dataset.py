"""Build immutable image training packages on the export queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import uuid
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
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    build_training_package,
)
from app.services.training_datasets import (
    MAX_IMAGE_BYTES,
    bind_training_authority,
    fail_dataset_build,
    mark_dataset_running,
    publish_dataset_package,
    update_dataset_progress,
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
    sample: dict, prefix: str, snapshot_digest: str, directory: Path
) -> dict:
    """Reuse preserved source bytes on retries; never reread changed originals."""
    key = f"{prefix}/media/{sample['task_id']}"
    bucket = storage_service.training_bucket
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
            for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
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
            image.load()
    except TrainingDatasetError:
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
    return {
        "task_id": sample["task_id"],
        "path": str(path),
        "sha256": digest,
        "size_bytes": size,
        "width": width,
        "height": height,
        "extension": extension,
    }


async def build_dataset(
    version_id: uuid.UUID, job_id: uuid.UUID, attempt_id: uuid.UUID, session_factory
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
            await _build_dataset(version_id, job_id, attempt_id, session_factory)
        finally:
            await execution.execute(select(func.pg_advisory_unlock(key)))
            await execution.commit()


async def _build_dataset(version_id, job_id, attempt_id, session_factory) -> None:
    prefix = None
    published = False
    async with session_factory() as db:
        version = await db.get(TrainingDatasetVersion, version_id)
        if version is None:
            return
        try:
            training_authority = bind_training_authority(db)
            await training_authority.assert_authority(
                project_id=version.project_id,
                actor_id=version.creator_id,
                api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
            )
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
            mark_dataset_running(version)
            await jobs.mark_running(db, job_id)
            snapshot = version.snapshot
            snapshot_digest = version.snapshot_digest
            prefix = f"projects/{version.project_id}/datasets/{version.id}"
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
                for index, sample in enumerate(snapshot["samples"]):
                    media.append(
                        await asyncio.to_thread(
                            materialize_image,
                            sample,
                            prefix,
                            snapshot_digest,
                            directory,
                        )
                    )
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
                    progress_pct = int(80 * (index + 1) / len(snapshot["samples"]))
                    update_dataset_progress(version, progress_pct)
                    await jobs.update_progress(db, job_id, progress_pct)
                    await db.commit()
                package = directory / "dataset.zip"
                manifest = await asyncio.to_thread(
                    build_training_package,
                    snapshot,
                    media,
                    package,
                    parent_manifest=parent_manifest,
                )
                package_key = f"{prefix}/attempts/{attempt_id}/dataset.zip"
                await asyncio.to_thread(
                    storage_service.upload_file,
                    str(package),
                    package_key,
                    bucket=storage_service.training_bucket,
                    content_type="application/zip",
                )
                package_digest = await asyncio.to_thread(file_digest, package)
                package_size = package.stat().st_size
                await training_authority.assert_authority(
                    project_id=version.project_id,
                    actor_id=version.creator_id,
                    api_key_id=version.snapshot.get("authorization", {}).get(
                        "api_key_id"
                    ),
                )
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
                publish_dataset_package(
                    version,
                    manifest=manifest,
                    package_key=package_key,
                    package_sha256=package_digest,
                    size_bytes=package_size,
                )
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
                error_code = (
                    exc.code
                    if isinstance(exc, TrainingDatasetError)
                    else "build_failed"
                )
                error_message = (
                    str(exc)
                    if isinstance(exc, TrainingDatasetError)
                    else "训练数据构建失败，请检查素材或重试"
                )
                fail_dataset_build(
                    version,
                    error_code=error_code,
                    error_message=error_message,
                )
                await jobs.mark_failed(db, job_id, error=error_message)
                await notify_job_terminal(db, job_id=job_id)
                await db.commit()
        finally:
            await db.rollback()
            if prefix and not published:
                try:
                    await asyncio.to_thread(
                        storage_service.delete_prefix,
                        f"{prefix}/attempts/{attempt_id}/",
                        bucket=storage_service.training_bucket,
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
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
