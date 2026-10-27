"""v0.11.15 · 外部连接器数据集导入 worker。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import uuid
from fastapi import HTTPException
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import Dataset
from app.db.models.storage_connection import StorageConnection
from app.services import async_job as async_job_svc, storage_access
from app.services.content_snapshot import ContentError
from app.services.storage_location import connector_location_id
from app.services.async_job_notify import notify_job_terminal
from app.services.dataset import DatasetService, IngestOutcome
from app.services.dataset_source_access import validate_dataset_write
from app.services.sources import build_adapter
from app.services.sources.base import SourceObject
from app.services.system_settings_service import SystemSettingsService
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(bind=True, name="app.workers.dataset_import.run_dataset_import")
def run_dataset_import(self, job_id: str) -> None:
    asyncio.run(
        _run_dataset_import(
            job_id=job_id,
            celery_task_id=getattr(self.request, "id", None),
        )
    )


async def _cancel_requested(db: AsyncSession, job_id: uuid.UUID) -> bool:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return True
    await db.refresh(job)
    if job.status == AsyncJobStatus.CANCELLED.value:
        return True
    return bool((job.payload or {}).get("cancel_requested"))


async def _finish_cancelled(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    result: dict,
) -> None:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status in {
        AsyncJobStatus.PENDING.value,
        AsyncJobStatus.RUNNING.value,
        AsyncJobStatus.CANCELLED.value,
    }:
        job.status = AsyncJobStatus.CANCELLED.value
        job.completed_at = datetime.now(timezone.utc)
        job.result = result


async def _resolve_import_limits_snapshot(
    db: AsyncSession, job: AsyncJob
) -> dict[str, Any]:
    """Load or persist the immutable budget used by this import job."""

    # Re-read under a row lock: two deliveries of a legacy queued job must not
    # both capture different live budgets and overwrite each other's snapshot.
    locked_job = await db.scalar(
        select(AsyncJob)
        .where(AsyncJob.id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_job is None:
        raise ValueError("dataset import job not found")
    job = locked_job
    payload = job.payload or {}
    snapshot = payload.get("settings_snapshot")
    if snapshot is None:
        # Jobs created before request-time snapshots get exactly one first-
        # execution capture. Persist before enumeration so retries reuse it.
        snapshot = await SystemSettingsService.get_import_limits_snapshot(db)
        job.payload = {**payload, "settings_snapshot": snapshot}
        await db.commit()
    if not isinstance(snapshot, dict):
        raise ValueError("invalid dataset import settings snapshot")
    max_files = snapshot.get("dataset_import_max_files")
    max_total_bytes = snapshot.get("dataset_import_max_total_bytes")
    version = snapshot.get("version")
    if (
        type(max_files) is not int
        or max_files <= 0
        or type(max_total_bytes) is not int
        or max_total_bytes <= 0
        or type(version) is not str
        or not version
    ):
        raise ValueError("invalid dataset import settings snapshot")
    return snapshot


def _collect_within_limits(
    objects: Iterable[SourceObject],
    *,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
) -> tuple[list[SourceObject], int]:
    """流式收集对象并在超限时立即短路抛错。

    避免对指向超大 bucket/目录的连接器先全量物化列表再判断限额——那样 worker
    可能在触达 max_files / max_total_bytes 之前就 OOM。这里在枚举过程中对计数与
    字节累加做短路：超限即抛 ValueError 中止，内存上界钳制在 max_files+1 条目。
    """
    # ``None`` is retained for direct legacy helper callers.  The worker path
    # always passes the persisted request snapshot explicitly.
    if max_files is None:
        max_files = settings.dataset_import_max_files
    if max_total_bytes is None:
        max_total_bytes = settings.dataset_import_max_total_bytes
    if type(max_files) is not int or max_files <= 0:
        raise ValueError("invalid import file-count limit")
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise ValueError("invalid import byte limit")

    collected: list[SourceObject] = []
    total_bytes = 0
    for obj in objects:
        collected.append(obj)
        total_bytes += max(0, obj.size)
        if len(collected) > max_files:
            raise ValueError(f"import file count exceeds limit (> {max_files})")
        if total_bytes > max_total_bytes:
            raise ValueError(f"import total bytes exceeds limit (> {max_total_bytes})")
    return collected, total_bytes


class _ImportByteBudget:
    """Charge actual source reads, including failed and deduplicated entries."""

    def __init__(self, limit: int):
        if type(limit) is not int or limit < 0:
            raise ValueError("invalid import byte limit")
        self.limit = limit
        self.consumed = 0

    @property
    def remaining(self):
        return max(0, self.limit - self.consumed)

    @property
    def exceeded(self):
        return self.consumed > self.limit

    def stream(self, stream):
        budget = self

        class BoundedStream:
            def read(self, size=-1):
                requested = 1024 * 1024 if size < 0 else size
                data = stream.read(min(requested, budget.remaining + 1))
                budget.consumed += len(data)
                if budget.exceeded:
                    raise ContentError("creation_byte_budget_exceeded", 413)
                return data

        return BoundedStream()


@asynccontextmanager
async def _registered_source(db, conn, revision):
    """Retain admission through stream ingestion and verified adapter closure."""
    async with await storage_access.acquire(
        db,
        connector_location_id(conn.id),
        "connection_import",
        expected_revision=revision,
    ):
        adapter = None

        def close_stream(stream):
            try:
                storage_access.close_registered(stream)
            except BaseException:
                raise storage_access.mark_unsettled(
                    ContentError("storage_operation_unsettled", 503)
                ) from None

        try:
            adapter = await build_adapter(db, conn)
            yield adapter, close_stream
        finally:
            # Constructors prove successful cleanup or propagate the explicit
            # uncertainty marker. Ordinary validation/read failures can settle.
            if adapter is not None:
                storage_access.close_registered(adapter)


async def _validate_connection_import(
    db, job, connection_id, revision, location_id, epoch, *, lock=False
):
    from app.services.dataset_source_access import require_manager
    from app.services.dataset_content import key_authority
    from app.services.storage_connection import assert_connection_usable

    actor = await require_manager(db, job.user_id)
    conn = await db.get(StorageConnection, connection_id, populate_existing=True)
    if conn is None:
        raise HTTPException(404, detail={"code": "access_revoked"})
    assert_connection_usable(actor, conn)
    if revision is not None and conn.revision != revision:
        raise HTTPException(409, detail={"code": "connection_revision_conflict"})
    proof = (job.payload or {}).get("connection_authority")
    if proof is not None and await key_authority(
        db, actor, proof.get("api_key_id"), "datasets:write"
    ) != proof.get("key_proof"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await storage_access.check(db, location_id, expected_revision=epoch, lock=lock)
    return conn


def _result(
    *,
    total: int,
    added: int,
    skipped: int,
    error_count: int,
    errors: list[dict],
    linked_tasks: int,
    cancelled_at_index: int | None = None,
    actual_read_bytes: int = 0,
) -> dict:
    out = {
        "total": total,
        "added": added,
        "imported": added,
        "skipped": skipped,
        "error_count": error_count,
        "errors": errors,
        "linked_tasks": linked_tasks,
        "actual_read_bytes": actual_read_bytes,
    }
    if cancelled_at_index is not None:
        out["cancelled_at_index"] = cancelled_at_index
    return out


async def _run_dataset_import(
    *,
    job_id: str,
    celery_task_id: str | None,
) -> None:
    job_uuid = uuid.UUID(job_id)
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            try:
                await async_job_svc.mark_running(
                    db, job_uuid, celery_task_id=celery_task_id
                )
                await db.commit()

                job = await db.get(AsyncJob, job_uuid)
                if job is None:
                    return
                payload = job.payload or {}
                settings_snapshot = await _resolve_import_limits_snapshot(db, job)
                max_files = settings_snapshot.get("dataset_import_max_files")
                max_total_bytes = settings_snapshot.get(
                    "dataset_import_max_total_bytes"
                )
                snapshot_version = settings_snapshot.get("version")
                log.debug(
                    "dataset_import using settings snapshot job=%s version=%s",
                    job_id,
                    snapshot_version,
                )
                await async_job_svc.update_progress(
                    db,
                    job_uuid,
                    1,
                    extra_payload={
                        "stage": "collecting",
                        "settings_version": snapshot_version,
                    },
                )
                await db.commit()
                dataset_id = uuid.UUID(str(payload["dataset_id"]))
                authority = payload.get("directory_authority")
                await validate_dataset_write(db, dataset_id, authority)
                connection_id = uuid.UUID(str(payload["connection_id"]))
                source_path = str(payload.get("source_path") or "")
                recursive = bool(payload.get("recursive", True))
                include_globs = payload.get("include_globs")
                if not isinstance(include_globs, list):
                    include_globs = []

                dataset = await db.get(Dataset, dataset_id)
                if dataset is None:
                    raise ValueError("dataset not found")
                location_id = connector_location_id(connection_id)
                maintenance_revision = payload.get("source_maintenance_revision", 1)
                connection_revision = payload.get("connection_revision")

                async def check_source(*, lock=False):
                    return await _validate_connection_import(
                        db,
                        job,
                        connection_id,
                        connection_revision,
                        location_id,
                        maintenance_revision,
                        lock=lock,
                    )

                conn = await check_source()
                async with _registered_source(db, conn, maintenance_revision) as source:
                    adapter, close_stream = source
                    objects, total_bytes = await storage_access.run(
                        db,
                        location_id,
                        "connection_listing",
                        lambda: _collect_within_limits(
                            adapter.list(source_path, recursive, include_globs),
                            max_files=max_files,
                            max_total_bytes=max_total_bytes,
                        ),
                        expected_revision=maintenance_revision,
                    )
                    total = len(objects)
                    await validate_dataset_write(db, dataset_id, authority)
                    await check_source(lock=True)

                    await async_job_svc.update_progress(
                        db,
                        job_uuid,
                        5 if total else 90,
                        extra_payload={
                            "stage": "importing",
                            "total_files": total,
                            "total_bytes": total_bytes,
                            "settings_version": snapshot_version,
                        },
                    )
                    await db.commit()

                    svc = DatasetService(db)
                    added = 0
                    skipped = 0
                    linked_tasks = 0
                    errors: list[dict] = []
                    error_count = 0
                    last_pct = 5
                    read_budget = _ImportByteBudget(max_total_bytes)

                    for index, obj in enumerate(objects, start=1):
                        await validate_dataset_write(db, dataset_id, authority)
                        await check_source()
                        if await _cancel_requested(db, job_uuid):
                            await _finish_cancelled(
                                db,
                                job_uuid,
                                result=_result(
                                    total=total,
                                    added=added,
                                    skipped=skipped + (total - index + 1),
                                    error_count=error_count,
                                    errors=errors,
                                    linked_tasks=linked_tasks,
                                    cancelled_at_index=index - 1,
                                    actual_read_bytes=read_budget.consumed,
                                ),
                            )
                            await notify_job_terminal(db, job_id=job_uuid)
                            await db.commit()
                            return

                        try:
                            stream = adapter.open(obj.relpath)
                            try:
                                # S3 适配器用 base_prefix，SFTP 用 base_path —— 取到哪个用哪个，
                                # 用于在用户把 source_path 写成含 base 的形式时对齐剥离。
                                adapter_base = getattr(adapter, "base_prefix", None)
                                if adapter_base is None:
                                    adapter_base = getattr(adapter, "base_path", "")
                                dest_rel = _dest_relpath(
                                    obj.relpath,
                                    source_path,
                                    adapter_base or "",
                                )
                                outcome = await svc.ingest_one(
                                    dataset_id,
                                    obj.relpath,
                                    read_budget.stream(stream),
                                    size=obj.size,
                                    dest_relpath=dest_rel or None,
                                    directory_authority=authority,
                                    max_bytes=read_budget.remaining,
                                    creation_requested_by=job.user_id,
                                    creation_authority={
                                        "job_id": str(job.id),
                                        "connection_id": str(connection_id),
                                        "connection_revision": connection_revision,
                                        "maintenance_revision": maintenance_revision,
                                        "directory_authority": authority,
                                        "connection_authority": payload.get(
                                            "connection_authority"
                                        ),
                                    },
                                )
                            finally:
                                close_stream(stream)
                            if read_budget.exceeded:
                                raise ContentError("creation_byte_budget_exceeded", 413)
                            await check_source(lock=True)
                            if outcome.status == "added":
                                added += 1
                                linked_tasks += outcome.linked_tasks
                                await db.commit()
                                if outcome.item_id is not None:
                                    try:
                                        await svc.enqueue_media_for_items(
                                            [outcome.item_id]
                                        )
                                    except Exception as exc:  # noqa: BLE001
                                        log.warning(
                                            "dataset_import media enqueue failed "
                                            "job=%s item=%s err=%s",
                                            job_id,
                                            outcome.item_id,
                                            exc,
                                        )
                            elif outcome.status == "skipped":
                                skipped += 1
                                await db.commit()
                            else:
                                error_count += 1
                                _append_error(errors, outcome)
                                await db.rollback()
                        except (HTTPException, ContentError):
                            raise
                        except Exception as exc:  # noqa: BLE001
                            if read_budget.exceeded:
                                raise ContentError(
                                    "creation_byte_budget_exceeded", 413
                                ) from None
                            await db.rollback()
                            error_count += 1
                            _append_error(
                                errors,
                                IngestOutcome(
                                    status="error",
                                    relpath=obj.relpath,
                                    error=f"{type(exc).__name__}: {exc}",
                                ),
                            )
                            log.warning(
                                "dataset_import item failed job=%s relpath=%s err=%s",
                                job_id,
                                obj.relpath,
                                exc,
                            )

                        pct = 5 + int(index / max(total, 1) * 90)
                        if pct >= last_pct + 5 or index == total:
                            await check_source(lock=True)
                            await async_job_svc.update_progress(
                                db,
                                job_uuid,
                                pct,
                                extra_payload={
                                    "actual_read_bytes": read_budget.consumed
                                },
                            )
                            await db.commit()
                            last_pct = pct

                    result = _result(
                        total=total,
                        added=added,
                        skipped=skipped,
                        error_count=error_count,
                        errors=errors,
                        linked_tasks=linked_tasks,
                        actual_read_bytes=read_budget.consumed,
                    )
                    await svc.assert_temporal_dataset_has_scenes(dataset_id)
                    await check_source(lock=True)
                    await async_job_svc.mark_complete(db, job_uuid, result=result)
                    await notify_job_terminal(db, job_id=job_uuid)
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                err = f"{type(exc).__name__}: {exc}"
                try:
                    await async_job_svc.mark_failed(db, job_uuid, error=err)
                    await notify_job_terminal(db, job_id=job_uuid)
                    await db.commit()
                except Exception:
                    await db.rollback()
                raise
    finally:
        await engine.dispose()


def _norm_rel(value: str) -> str:
    return (value or "").replace("\\", "/").strip().strip("/")


def _dest_relpath(relpath: str, source_path: str, base_prefix: str = "") -> str:
    """把「相对连接器根」的 relpath 转成「相对 source_path」的子路径，保留目录层级。

    obj.relpath 已剥掉 base_prefix；source_path 是用户输入（base 相对）。剥掉 source_path
    这段前缀，避免导入后多嵌套一级（`{dataset}/dataset-A/a/x` → `{dataset}/a/x`）。
    base_prefix 用于兜底：用户把 source_path 写成含 base 的形式时也能对齐。返回空串表示该对象
    正好等于 source_path（单文件），交由 ingest_one 退回 basename。
    """
    rel = _norm_rel(relpath)
    src = _norm_rel(source_path)
    base = _norm_rel(base_prefix)
    if base and (src == base or src.startswith(f"{base}/")):
        src = src[len(base) :].lstrip("/")
    if src and (rel == src or rel.startswith(f"{src}/")):
        rel = rel[len(src) :].lstrip("/")
    return rel


def _append_error(errors: list[dict], outcome: IngestOutcome) -> None:
    if len(errors) >= 100:
        return
    errors.append(
        {
            "relpath": outcome.relpath,
            "error": outcome.error or outcome.reason or "unknown error",
        }
    )
