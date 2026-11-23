"""Publish verified audit archives before atomically removing their exact source."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from tempfile import TemporaryFile
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from yoao_audit.maintenance_config import load_archive_s3_config
from yoao_audit.maintenance_contract import AuditPartition
from yoao_audit.maintenance_postgres import (
    AuditMaintenancePostgresData,
    platform_maintenance,
)
from yoao_audit.partitions import AuditPartitionManager

from app.config import settings
from app.services.storage import storage_service
from app.db.models.async_job import AsyncJob, AsyncJobKind, AsyncJobStatus
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.services import async_job as jobs
from app.services import audit_archive_storage as archive_storage
from app.services import conditional_write_accounting
from app.services import saved_output_io
from app.services import storage_access
from app.services.audit import AuditAction, AuditService
from app.services.audit_archive_output_data import (
    has_ready_output,
    lock_pending_output,
    save_candidate,
)
from app.services.audit_archive_partitions import (
    AuditArchiveCancelled,
    AuditArchiveError,
    SourcePartition,
    build_archive,
    catalog,
    ensure_month,
    lock_retirement,
    lock_source,
    month_mutex,
    next_month,
    partition_name,
    publish_source,
    retention_cutoff,
    stream_source,
    verify_source,
)
from app.services.jobs_lifecycle_data import job_lock_statement


class AuditArchiveReadService:
    @staticmethod
    @contextmanager
    def _archive_read_manager() -> Iterator[AuditPartitionManager]:
        prefix = "AUDIT_ARCHIVE_READ_S3"
        config = load_archive_s3_config(
            {
                f"{prefix}_ENDPOINT_URL": settings.audit_archive_read_s3_endpoint_url,
                f"{prefix}_REGION": settings.audit_archive_read_s3_region,
                f"{prefix}_BUCKET": settings.audit_archive_read_s3_bucket,
                f"{prefix}_ACCESS_KEY_ID": settings.audit_archive_read_s3_access_key_id,
                f"{prefix}_ACCESS_KEY_ID_FILE": settings.audit_archive_read_s3_access_key_id_file,
                f"{prefix}_SECRET_ACCESS_KEY": settings.audit_archive_read_s3_secret_access_key,
                f"{prefix}_SECRET_ACCESS_KEY_FILE": settings.audit_archive_read_s3_secret_access_key_file,
            },
            prefix=prefix,
        )
        if config is None:
            yield AuditPartitionManager(None, storage_service)
            return

        storage = config.archive_storage()
        try:
            yield AuditPartitionManager(None, storage)
        finally:
            storage.close()

    @staticmethod
    def _manager(db: AsyncSession) -> AuditPartitionManager:
        async def record_archive(
            partition: AuditPartition, row_count: int, object_key: str
        ) -> None:
            await AuditService.log(
                db,
                actor=None,
                action=AuditAction.AUDIT_ARCHIVE,
                target_type="audit_partition",
                target_id=partition.name,
                request=None,
                status_code=200,
                detail={
                    "partition": partition.name,
                    "row_count": row_count,
                    "archive_key": object_key,
                    "from": partition.range_from.isoformat(),
                },
            )

        return AuditPartitionManager(
            AuditMaintenancePostgresData(db),
            storage_service,
            record_archive=record_archive,
        )

    @staticmethod
    async def ensure_future_partitions(
        db: AsyncSession, *, months_ahead: int = 3
    ) -> list[str]:
        return await AuditArchiveReadService._manager(db).ensure_future_partitions(
            months_ahead=months_ahead
        )

    @staticmethod
    async def archive_old_partitions(
        db: AsyncSession, *, retain_months: int = 12
    ) -> dict:
        return await AuditArchiveReadService._manager(db).archive_old_partitions(
            retain_months=retain_months
        )

    @staticmethod
    async def refresh_daily(db: AsyncSession) -> None:
        await platform_maintenance(db, "refresh_daily")

    @staticmethod
    def list_archives() -> list[dict]:
        with AuditArchiveReadService._archive_read_manager() as manager:
            return manager.list_archives()

    @staticmethod
    def read_archive_rows(year: int, month: int) -> list[dict] | None:
        with AuditArchiveReadService._archive_read_manager() as manager:
            return manager.read_archive_rows(year, month)


def _empty_result() -> dict:
    return {
        "archived_partitions": [],
        "archive_keys": [],
        "output_ids": [],
        "total_rows": 0,
    }


def _result(current: dict | None) -> dict:
    """Preserve only the committed archive receipt, including earlier months."""
    if not current:
        return _empty_result()
    result = {}
    for field in ("archived_partitions", "archive_keys", "output_ids"):
        value = current.get(field, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise AuditArchiveError("audit_archive_job_result_invalid")
        result[field] = list(value)
    total = current.get("total_rows", 0)
    if type(total) is not int or total < 0:
        raise AuditArchiveError("audit_archive_job_result_invalid")
    result["total_rows"] = total
    return result


def _not_cancelled() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def _begin(db: AsyncSession) -> None:
    """Select isolation before the first statement of every owned transaction."""
    if db.in_transaction():
        raise AuditArchiveError("audit_archive_transaction_required")
    await db.connection(execution_options={"isolation_level": "READ COMMITTED"})
    await db.execute(text("SET LOCAL lock_timeout = '1s'"))
    await db.execute(text("SET LOCAL statement_timeout = '600s'"))
    if await db.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise AuditArchiveError("audit_archive_transaction_required")


async def _rollback(db: AsyncSession) -> None:
    await storage_access.finish(asyncio.create_task(db.rollback()))


async def _running_job(
    db: AsyncSession, job_id: UUID, *, lock: bool = True
) -> AsyncJob:
    _not_cancelled()
    job = (
        await db.scalar(
            job_lock_statement(job_id, mode="update", populate_existing=True)
        )
        if lock
        else await jobs.get_job(db, job_id, populate_existing=True)
    )
    if job is None or job.kind != AsyncJobKind.AUDIT_ARCHIVE.value:
        raise AuditArchiveError("audit_archive_job_unavailable")
    if job.status == AsyncJobStatus.CANCELLED.value or (job.payload or {}).get(
        "cancel_requested"
    ):
        raise AuditArchiveCancelled()
    if job.status != AsyncJobStatus.RUNNING.value:
        raise AuditArchiveError("audit_archive_job_unavailable")
    return job


async def _ready_exists(db: AsyncSession, source: SourcePartition) -> None:
    if await has_ready_output(db, source.year, source.month):
        raise AuditArchiveError("audit_archive_month_already_ready")


async def _pending_owner(db: AsyncSession, receipt) -> None:
    row = await lock_pending_output(db, receipt.id)
    if row is None or row.state != "pending" or not archive_storage.same(row, receipt):
        raise AuditArchiveError("audit_archive_owner_changed")


async def _candidate(db: AsyncSession, source: SourcePartition, job_id: UUID, file):
    await _begin(db)
    await month_mutex(db, source.month_start)
    await _running_job(db, job_id, lock=False)
    await _ready_exists(db, source)
    await lock_source(db, source)
    row = AuditArchiveOutput(
        id=uuid4(),
        job_id=job_id,
        year=source.year,
        month=source.month,
        source_schema="public",
        source_table=source.name,
        parent_oid=source.parent_oid,
        source_oid=source.source_oid,
        month_start=source.month_start,
        month_end=source.month_end,
        format_revision=1,
        state="pending",
        created_at=datetime.now(timezone.utc),
    )
    await build_archive(db, source, row, file)
    row.storage_ref = await archive_storage.allocate(db, row)
    await save_candidate(db, row)
    await db.flush()
    receipt = archive_storage.snapshot(row)
    await _running_job(db, job_id)
    _not_cancelled()
    await db.commit()
    return receipt


@dataclass
class _WriteAccounting:
    recorder: object
    permit: object = None


async def _write_candidate(db: AsyncSession, receipt, file, *, accounting=None) -> int:
    revision = None
    target = await archive_storage.io_target(db, receipt)
    source = saved_output_io.capture(target, "archive", receipt)
    if target is not None:
        await db.rollback()

    async def acquired(permit):
        nonlocal revision
        if accounting is not None:
            accounting.permit = permit
        if source is not None:
            source.attach(permit)
        await _begin(db)
        await _running_job(db, receipt.job_id)
        await _pending_owner(db, receipt)
        await storage_access.check(
            db,
            UUID(str(receipt.storage_ref["location_id"])),
            expected_revision=permit.revision,
            lock=True,
        )
        _not_cancelled()
        revision = permit.revision
        await db.commit()

    await storage_access.run(
        db,
        UUID(str(receipt.storage_ref["location_id"])),
        f"audit_archive_write_{receipt.id.hex}",
        archive_storage.write_from_source if source is not None else archive_storage.write,
        receipt,
        file,
        **({"source": source} if source is not None else {}),
        **({"target": target} if target is not None and source is None else {}),
        **({"accounting": accounting.recorder} if accounting is not None else {}),
        timeout=archive_storage.VERIFY_SECONDS,
        on_acquired=acquired,
    )
    if revision is None:
        raise AuditArchiveError("audit_archive_admission_unavailable", 503)
    return revision


async def _archive_partition(db, source, job_id, candidate_count):
    accounting, failure, input_settled = None, None, False
    file = TemporaryFile(mode="w+b")
    try:
        receipt = await _candidate(db, source, job_id, file)
        accounting = _WriteAccounting(
            conditional_write_accounting.recorder(
                db,
                receipt,
                producer="audit_archive_outputs",
                purpose="other_platform",
            )
        )
        revision = await _write_candidate(db, receipt, file, accounting=accounting)
        result = await _publish(db, source, receipt, revision, candidate_count)
    except BaseException as exc:
        failure = exc
    finally:
        try:
            file.close()
            input_settled = True
        except BaseException as closing:
            failure = storage_access.mark_unsettled(closing)
        if accounting is not None:
            failure = await conditional_write_accounting.finish(
                accounting.recorder,
                failure,
                settled=input_settled
                and getattr(accounting.permit, "_settled", False) is True,
            )
    if failure is not None:
        raise failure
    return result


async def _publish(
    db: AsyncSession,
    source: SourcePartition,
    receipt,
    revision: int,
    candidate_count: int,
) -> dict:
    await _begin(db)
    await month_mutex(db, source.month_start)
    await _running_job(db, receipt.job_id)
    await _pending_owner(db, receipt)
    await _ready_exists(db, source)
    await lock_source(db, source)
    observed = await stream_source(db, source, expected_rows=receipt.row_count)
    if (
        observed.size_bytes != receipt.source_size_bytes
        or observed.sha256 != receipt.source_sha256
    ):
        raise AuditArchiveError("audit_archive_source_changed")
    await lock_retirement(db, source)
    await storage_access.check(
        db,
        UUID(str(receipt.storage_ref["location_id"])),
        expected_revision=revision,
        lock=True,
    )
    job = await _running_job(db, receipt.job_id)
    await _pending_owner(db, receipt)
    _not_cancelled()
    await publish_source(db, source, receipt.id, datetime.now(timezone.utc))
    key = archive_storage.key_for(source.year, source.month, receipt.id)
    await AuditService.log(
        db,
        actor=None,
        action=AuditAction.AUDIT_ARCHIVE,
        target_type="audit_partition",
        target_id=source.name,
        request=None,
        status_code=200,
        detail={
            "partition": source.name,
            "archive_output_id": str(receipt.id),
            "archive_key": key,
            "row_count": receipt.row_count,
            "source_sha256": receipt.source_sha256,
            "from": source.month_start.isoformat(),
        },
    )
    result = _result(job.result)
    result["archived_partitions"].append(source.name)
    result["archive_keys"].append(key)
    result["output_ids"].append(str(receipt.id))
    result["total_rows"] += receipt.row_count
    await jobs.update_progress(
        db,
        job.id,
        min(99, len(result["output_ids"]) * 100 // candidate_count),
        result=result,
    )
    await db.flush()
    _not_cancelled()
    await db.commit()
    return result


def _classify(exc: Exception) -> AuditArchiveError:
    if isinstance(exc, AuditArchiveError):
        return exc
    if isinstance(exc, archive_storage.AuditArchiveStorageError):
        return AuditArchiveError(exc.code, exc.status_code)
    if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
        return AuditArchiveError("audit_archive_busy")
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.startswith("storage_"):
        return AuditArchiveError(
            f"audit_archive_{code}", getattr(exc, "status_code", 409)
        )
    return AuditArchiveError("audit_archive_unavailable", 503)


class AuditPartitionService:
    @staticmethod
    async def ensure_future_partitions(
        db: AsyncSession, *, months_ahead: int = 3
    ) -> list[str]:
        """Create exact UTC month leaves without adopting same-named tables."""
        if type(months_ahead) is not int or months_ahead < 0:
            raise AuditArchiveError("audit_archive_invalid_months_ahead", 422)
        try:
            current = datetime.now(timezone.utc).date().replace(day=1)
            children = {source.name: source for source in await catalog(db)}
            created = []
            for _ in range(months_ahead + 1):
                await month_mutex(db, current)
                name = partition_name(current)
                if name in children:
                    await verify_source(db, children[name])
                else:
                    await ensure_month(db, current)
                    observed = {source.name: source for source in await catalog(db)}
                    if name not in observed or observed[name].month_start != current:
                        raise AuditArchiveError("audit_archive_source_invalid")
                    children = observed
                    created.append(name)
                current = next_month(current)
            return created
        except Exception as exc:
            raise _classify(exc) from None

    @staticmethod
    async def archive_old_partitions(
        db: AsyncSession,
        *,
        retain_months: int = 12,
        job_id: UUID | None = None,
    ) -> dict:
        """Own fresh transactions; leave every failed candidate pending for review."""
        cutoff = retention_cutoff(datetime.now(timezone.utc).date(), retain_months)
        fresh = isinstance(db.bind, AsyncEngine) and not db.in_transaction()
        try:
            if fresh:
                await _begin(db)
            children = [
                source for source in await catalog(db) if source.month_end <= cutoff
            ]
            if not children:
                if fresh:
                    await db.commit()
                return _empty_result()
            if not fresh:
                raise AuditArchiveError("audit_archive_transaction_required")
            if job_id is None or not isinstance(job_id, UUID):
                raise AuditArchiveError("audit_archive_job_required", 422)
            await db.commit()
            result = _empty_result()
            for source in children:
                result = await _archive_partition(db, source, job_id, len(children))
            return result
        except BaseException as exc:
            if fresh:
                try:
                    await _rollback(db)
                except BaseException:
                    if not isinstance(exc, Exception):
                        raise exc from None
                    raise AuditArchiveError("audit_archive_unavailable", 503) from None
            if isinstance(exc, Exception):
                raise _classify(exc) from None
            raise
