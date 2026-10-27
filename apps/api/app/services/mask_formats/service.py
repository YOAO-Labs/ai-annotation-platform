from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_jobs.dispatch_contract import DispatchLease

from app.config import settings
from app.db.models.async_job import AsyncJobKind
from app.db.models.mask_format_upload import MaskFormatUpload
from app.db.models.project import Project
from app.schemas.mask_format import (
    MaskFormatImportBatchOut,
    MaskFormatImportPreflightResponse,
)
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.mask_formats.contracts import StagedObject, canonical_digest
from app.services.mask_formats.catalog import registry
from app.services.storage import storage_service
from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    CancelPendingMaskFormatImport,
    CreateMaskFormatImport,
    FinishMaskFormatImport,
    LinkMaskFormatImportJob,
    LockMaskFormatImport,
    LockMaskFormatImportReceipt,
    MarkMaskFormatImportRunning,
    MaskFormatImportSnapshot,
    RecordMaskFormatImportItem,
    ReadMaskFormatImport,
)
from app.services.labeling_annotation_transfer import (
    AnnotationTransferRejected,
    annotation_transfer_command,
)


RECEIPT_TTL = timedelta(minutes=15)
STAGING_IO_SECONDS = 300


class MaskFormatError(ValueError):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status_code: int = 409,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}


async def _transfer(db: AsyncSession, command):
    try:
        return await annotation_transfer_command(db, command)
    except AnnotationTransferRejected as exc:
        raise MaskFormatError(
            exc.reason,
            str(exc),
            status_code=exc.status_code,
            detail=exc.detail if isinstance(exc.detail, dict) else None,
        ) from exc


def _storage_failure(exc):
    code = getattr(exc, "code", None)
    status_code = getattr(exc, "status_code", getattr(exc, "status", None))
    if code is None or status_code is None:
        raise exc
    error = MaskFormatError(code, "导入暂存位置或内容无法核实", status_code=status_code)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


async def storage_call(call, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except MaskFormatError:
        raise
    except ValueError as exc:
        raise _storage_failure(exc) from None


def staged_prefix(project_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"mask-formats/{project_id}/{user_id}/"


def _token_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode()).hexdigest()


def _assert_staged_key(
    object_key: str,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    prefix = staged_prefix(project_id, user_id)
    if not object_key.startswith(prefix) or object_key == prefix:
        raise MaskFormatError(
            "staged_object_scope_mismatch",
            "staged object does not belong to this project and user",
            status_code=403,
        )


def staged_parts(object_key: str) -> tuple[str, str]:
    prefix, separator, relative = object_key.rpartition("/")
    if not separator or not relative:
        raise MaskFormatError("staged_object_scope_mismatch", "invalid staged key")
    return prefix + "/", relative


async def staging_owner(db, object_key, staging_ref=None):
    """Resolve a saved address; NULL old batches always mean the legacy address."""
    from app.services import import_storage

    prefix, relative = staged_parts(object_key)
    try:
        ref = (
            staging_ref
            if staging_ref is not None
            else import_storage.legacy_ref(prefix)
        )
        owner = import_storage.resolve_ref(ref, expected_prefix=prefix)
        owner.key(relative)
    except ValueError as exc:
        raise _storage_failure(exc) from None
    return owner


async def _upload_source(db, object_key, project_id, user_id, *, lock=False):
    query = select(MaskFormatUpload).where(
        MaskFormatUpload.logical_object_key == object_key
    )
    if lock:
        query = query.with_for_update(read=True, nowait=True)
    try:
        row = await db.scalar(query.execution_options(populate_existing=True))
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            raise MaskFormatError(
                "staged_upload_busy", "staged upload is busy"
            ) from None
        raise
    if row is None:
        # Old upload-init returned only this logical namespace, without a row.
        owner = await staging_owner(db, object_key)
        return owner.ref, None
    if row.project_id != project_id or row.uploader_id != user_id:
        raise MaskFormatError(
            "staged_object_scope_mismatch",
            "staged upload owner changed",
            status_code=403,
        )
    owner = await staging_owner(db, object_key, row.storage_ref)
    facts = tuple(
        deepcopy(getattr(row, name))
        for name in (
            "id",
            "project_id",
            "uploader_id",
            "logical_object_key",
            "content_type",
            "storage_ref",
            "created_at",
            "upload_expires_at",
        )
    )
    return owner.ref, facts


async def check_staging(db, object_key, staging_ref, *, revision=None, lock=False):
    from app.services import import_storage

    owner = await staging_owner(db, object_key, staging_ref)
    return await storage_call(
        import_storage.check_location, db, owner, expected_revision=revision, lock=lock
    )


async def _check_staging_read(
    db, object_key, staging_ref, *, revision=None, read_route=None, lock=False
):
    """Recheck the captured read route without granting upload permission."""
    if read_route is None:
        return await check_staging(
            db, object_key, staging_ref, revision=revision, lock=lock
        )
    owner = await staging_owner(db, object_key, staging_ref)
    _, relative = staged_parts(object_key)
    if (
        read_route.source_location_id,
        read_route.source_endpoint_url,
        read_route.source_bucket,
        read_route.source_object_key,
    ) != (
        owner.location_id,
        owner.ref["endpoint_url"],
        owner.bucket,
        owner.key(relative),
    ):
        raise MaskFormatError("read_publication_changed", "暂存读取位置已变更")
    await storage_call(read_publications.recheck, db, read_route, lock=lock)


def read_staged_from_client(client, owner, object_key, expected_sha256, local_path):
    """Read a published physical copy while retaining its logical saved owner."""
    return _materialize(
        None, owner, object_key, expected_sha256, local_path, reader=client
    )


def _materialize(
    target, owner, object_key, expected_sha256, local_path, *, reader=None
):
    from app.services import import_storage

    _, relative = staged_parts(object_key)
    physical_key = owner.key(relative)
    deadline = time.monotonic() + STAGING_IO_SECONDS

    def budget():
        if time.monotonic() >= deadline:
            raise MaskFormatError(
                "staged_object_timeout", "staged read timed out", status_code=503
            )

    try:
        context = (
            nullcontext(reader)
            if reader is not None
            else import_storage.client_context(target)
        )
        with context as client:
            budget()
            if owner.ref["schema"] == 1 and reader is None:
                # Preserve the old unversioned contract; SHA remains authoritative.
                meta = storage_service.verify_upload(physical_key, bucket=owner.bucket)
            else:
                meta = client.head_object(Bucket=owner.bucket, Key=physical_key)
            if meta is None:
                raise MaskFormatError(
                    "staged_object_missing", "staged object is missing", status_code=404
                )
            budget()
            content_length = int(meta.get("ContentLength") or 0)
            if content_length > settings.mask_format_temp_quota_bytes:
                raise _quota(
                    content_length, "staged object exceeds the temporary byte quota"
                )
            params = {"Bucket": owner.bucket, "Key": physical_key}
            if meta.get("ETag"):
                params["IfMatch"] = meta["ETag"]
            if meta.get("VersionId") not in {None, "null"}:
                params["VersionId"] = meta["VersionId"]
            response = client.get_object(**params)
            digest, written = hashlib.sha256(), 0
            with storage_access.closing_registered(response["Body"]) as body:
                with local_path.open("wb") as sink:
                    while True:
                        budget()
                        chunk = body.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > settings.mask_format_temp_quota_bytes:
                            raise _quota(
                                written,
                                "staged object exceeded the streaming byte quota",
                            )
                        digest.update(chunk)
                        sink.write(chunk)
            budget()
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise MaskFormatError(
                    "staged_object_digest_conflict",
                    "staged object digest changed after preflight",
                    detail={
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )
            return StagedObject(
                object_key=object_key,
                sha256=actual_sha256,
                local_path=os.fspath(local_path),
                size_bytes=written,
            )
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise MaskFormatError(
                "staged_object_missing", "staged object is missing", status_code=404
            ) from None
        if code in {"412", "PreconditionFailed"}:
            raise MaskFormatError(
                "staged_object_digest_conflict", "staged object changed"
            ) from None
        raise MaskFormatError(
            "staged_object_unavailable", "staged object is unavailable", status_code=503
        ) from None


def _quota(observed, message):
    return MaskFormatError(
        "resource_budget_exceeded",
        message,
        status_code=413,
        detail={
            "budget": "mask_format_temp_quota_bytes",
            "limit": settings.mask_format_temp_quota_bytes,
            "observed": observed,
        },
    )


@asynccontextmanager
async def materialize_staged_object(
    db: AsyncSession,
    *,
    object_key: str,
    expected_sha256: str,
    staging_ref: dict | None = None,
    observation: dict | None = None,
) -> AsyncIterator[StagedObject]:
    from app.services import import_storage

    sessions = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with sessions() as reading:
        owner = await staging_owner(reading, object_key, staging_ref)
        _, relative = staged_parts(object_key)
        read_route = await storage_call(
            migration_reads.resolve,
            reading,
            owner.ref,
            owner.key(relative),
            expected_sha256,
            source_version_id=read_publications.ANY_VERSION,
        )
        target, revision = None, None
        if read_route is None:
            target = await storage_call(import_storage.io_target, reading, owner)
            revision = await storage_call(import_storage.check_location, reading, owner)
        await reading.rollback()
        # No caller authority or batch row lock is held during physical I/O.
        with tempfile.TemporaryDirectory(prefix="aap-mask-format-") as temp_dir:
            local_path = Path(temp_dir) / "staged-object"
            if read_route is None:
                staged = await storage_call(
                    storage_access.run,
                    reading,
                    owner.location_id,
                    "mask_format_staging",
                    _materialize,
                    target,
                    owner,
                    object_key,
                    expected_sha256,
                    local_path,
                    expected_revision=revision,
                    timeout=STAGING_IO_SECONDS,
                )
            else:
                staged = await storage_call(
                    migration_reads.run,
                    reading,
                    read_route,
                    "mask_format_staging",
                    lambda client: read_staged_from_client(
                        client, owner, object_key, expected_sha256, local_path
                    ),
                    timeout=STAGING_IO_SECONDS,
                )
            await _check_staging_read(
                reading,
                object_key,
                owner.ref,
                revision=revision,
                read_route=read_route,
            )
            await reading.rollback()
            if observation is not None:
                observation.update(revision=revision, read_route=read_route)
            yield staged


def batch_out(batch: MaskFormatImportSnapshot) -> MaskFormatImportBatchOut:
    return MaskFormatImportBatchOut(
        id=batch.id,
        project_id=batch.project_id,
        async_job_id=batch.async_job_id,
        format_id=batch.format_id,
        adapter_version=batch.adapter_version,
        manifest_version=batch.manifest_version,
        staged_sha256=batch.staged_sha256,
        plan_digest=batch.plan_digest,
        status=batch.status,
        result=dict(batch.result_json or {}),
        receipt_expires_at=batch.receipt_expires_at,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
    )


async def preflight_import(
    db: AsyncSession,
    *,
    project: Project,
    user_id: uuid.UUID,
    format_id: str,
    staged_object_key: str,
    staged_sha256: str,
    mapping: dict[str, Any],
    options: dict[str, Any],
    before_publish=None,
) -> MaskFormatImportPreflightResponse:
    try:
        adapter = registry.get(format_id)
    except ValueError as exc:
        raise MaskFormatError(
            "format_adapter_not_found", str(exc), status_code=404
        ) from exc
    capability = adapter.descriptor.import_capability
    if (
        not capability.supported
        or (project.data_type or "image") not in adapter.descriptor.media_types
    ):
        raise MaskFormatError(
            "format_import_unsupported",
            "format adapter does not support this project media type",
            status_code=422,
        )
    _assert_staged_key(
        staged_object_key,
        project_id=project.id,
        user_id=user_id,
    )
    staging_ref, upload_facts = await _upload_source(
        db, staged_object_key, project.id, user_id
    )
    # The dependency's initial authority transaction ends before provider I/O.
    await db.commit()
    observation = {}
    async with materialize_staged_object(
        db,
        object_key=staged_object_key,
        expected_sha256=staged_sha256,
        staging_ref=staging_ref,
        observation=observation,
    ) as staged:
        try:
            plan = await adapter.preflight_import(
                db,
                project=project,
                staged=staged,
                mapping=mapping,
                options=options,
            )
        except MaskFormatError:
            raise
        except ValueError as exc:
            raise MaskFormatError(
                "format_preflight_failed", str(exc), status_code=422
            ) from exc

    if before_publish is not None:
        await before_publish()
    await _check_staging_read(
        db,
        staged_object_key,
        staging_ref,
        revision=observation["revision"],
        read_route=observation.get("read_route"),
        lock=True,
    )
    current_ref, current_facts = await _upload_source(
        db, staged_object_key, project.id, user_id, lock=True
    )
    if current_ref != staging_ref or current_facts != upload_facts:
        raise MaskFormatError(
            "staged_object_owner_changed", "staged upload owner changed"
        )
    receipt = f"mfi_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(timezone.utc) + RECEIPT_TTL
    data = await _transfer(
        db,
        CreateMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="create_mask_format_import",
            id=uuid.uuid4(),
            project_id=project.id,
            requested_by_id=user_id,
            format_id=format_id,
            adapter_version=adapter.descriptor.adapter_version,
            manifest_version=adapter.descriptor.manifest_version,
            staged_object_key=staged_object_key,
            staged_sha256=staged_sha256,
            mapping_json=mapping,
            options_json=options,
            mapping_digest=canonical_digest(mapping),
            options_digest=canonical_digest(options),
            plan_json=plan.model_dump(mode="json"),
            plan_digest=plan.plan_digest,
            token_hash=_token_hash(receipt),
            receipt_expires_at=expires_at,
            result_json={"items": {}},
        ),
    )
    assert data.mask_import is not None
    return MaskFormatImportPreflightResponse(
        import_id=data.mask_import.id,
        receipt=receipt,
        receipt_expires_at=expires_at,
        plan=plan,
    )


async def execute_import(
    db: AsyncSession,
    *,
    project: Project,
    user_id: uuid.UUID,
    receipt: str,
    plan_digest: str,
    confirm_lossy: bool,
) -> tuple[MaskFormatImportSnapshot, bool]:
    data = await _transfer(
        db,
        LockMaskFormatImportReceipt(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_mask_format_import_receipt",
            token_hash=_token_hash(receipt),
            project_id=project.id,
            requested_by_id=user_id,
        ),
    )
    assert data.mask_import is not None
    batch = data.mask_import
    if batch.plan_digest != plan_digest:
        raise MaskFormatError(
            "format_plan_digest_conflict", "format plan digest changed"
        )
    if batch.receipt_expires_at <= datetime.now(timezone.utc):
        raise MaskFormatError(
            "format_receipt_expired",
            "format import receipt has expired",
            status_code=410,
        )
    adapter = registry.get(batch.format_id)
    if (
        batch.adapter_version != adapter.descriptor.adapter_version
        or batch.manifest_version != adapter.descriptor.manifest_version
    ):
        raise MaskFormatError(
            "format_adapter_version_conflict",
            "format adapter changed after preflight",
        )
    if canonical_digest(batch.mapping_json or {}) != batch.mapping_digest:
        raise MaskFormatError(
            "format_mapping_digest_conflict", "format mapping changed"
        )
    if canonical_digest(batch.options_json or {}) != batch.options_digest:
        raise MaskFormatError(
            "format_options_digest_conflict", "format options changed"
        )
    plan = batch.plan_json or {}
    if not any(
        item.get("loss_class") in {"lossless", "lossy"}
        for item in plan.get("items", [])
        if isinstance(item, dict)
    ):
        raise MaskFormatError(
            "format_plan_has_no_executable_items",
            "format import plan has no executable items",
            status_code=422,
        )
    if plan.get("loss_class") == "lossy" and not confirm_lossy:
        raise MaskFormatError(
            "format_lossy_confirmation_required",
            "lossy format import requires explicit confirmation",
            status_code=422,
        )
    if batch.async_job_id is not None:
        return batch, False
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.MASK_FORMAT_IMPORT.value,
        project_id=project.id,
        user_id=user_id,
        payload={
            "mask_format_import_id": str(batch.id),
            "format": batch.format_id,
            "staged_sha256": batch.staged_sha256,
            "plan_digest": batch.plan_digest,
            "project_display_id": project.display_id,
        },
    )
    linked = await _transfer(
        db,
        LinkMaskFormatImportJob(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="link_mask_format_import_job",
            id=batch.id,
            project_id=project.id,
            requested_by_id=user_id,
            async_job_id=job.id,
            resume=False,
        ),
    )
    assert linked.mask_import is not None
    return linked.mask_import, True


async def resume_import(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> MaskFormatImportSnapshot:
    data = await _transfer(
        db,
        LockMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_mask_format_import",
            id=batch_id,
            project_id=project_id,
            async_job_id=None,
        ),
    )
    batch = data.mask_import
    if batch is None:
        raise MaskFormatError(
            "format_import_not_found", "format import not found", status_code=404
        )
    if batch.requested_by_id != user_id:
        raise MaskFormatError(
            "format_import_not_found", "format import not found", status_code=404
        )
    if batch.status not in {"failed", "partial"}:
        raise MaskFormatError(
            "format_import_not_resumable",
            "only failed or partial imports can be resumed",
            status_code=422,
        )
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.MASK_FORMAT_IMPORT.value,
        project_id=batch.project_id,
        user_id=user_id,
        payload={
            "mask_format_import_id": str(batch.id),
            "format": batch.format_id,
            "staged_sha256": batch.staged_sha256,
            "plan_digest": batch.plan_digest,
            "resumed": True,
        },
    )
    linked = await _transfer(
        db,
        LinkMaskFormatImportJob(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="link_mask_format_import_job",
            id=batch.id,
            project_id=batch.project_id,
            requested_by_id=user_id,
            async_job_id=job.id,
            resume=True,
        ),
    )
    assert linked.mask_import is not None
    return linked.mask_import


async def prepare_import_dispatch(
    db: AsyncSession, batch: MaskFormatImportSnapshot
) -> DispatchLease:
    from app.services.jobs_dispatch import prepare_job_dispatch

    assert batch.async_job_id is not None
    await db.flush()
    return await prepare_job_dispatch(
        db,
        job_id=batch.async_job_id,
        task_name="app.workers.mask_format_import.run_mask_format_import",
        args=[str(batch.id)],
    )


async def dispatch_import(
    batch_id: uuid.UUID, *, dispatcher: QualityJobDispatcher, execution_id: str
) -> str:
    try:
        return dispatcher.dispatch_mask_import(batch_id, execution_id=execution_id)
    except Exception as exc:
        raise MaskFormatError(
            "format_import_dispatch_failed",
            "format import worker dispatch failed",
            status_code=503,
        ) from exc


async def read_import(
    db: AsyncSession, *, import_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> MaskFormatImportSnapshot | None:
    data = await _transfer(
        db,
        ReadMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="read_mask_format_import",
            id=import_id,
            project_id=project_id,
        ),
    )
    return data.mask_import


async def lock_import(
    db: AsyncSession,
    *,
    import_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    async_job_id: uuid.UUID | None = None,
) -> MaskFormatImportSnapshot | None:
    data = await _transfer(
        db,
        LockMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_mask_format_import",
            id=import_id,
            project_id=project_id,
            async_job_id=async_job_id,
        ),
    )
    return data.mask_import


async def mark_import_running(
    db: AsyncSession, batch: MaskFormatImportSnapshot
) -> MaskFormatImportSnapshot:
    data = await _transfer(
        db,
        MarkMaskFormatImportRunning(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="mark_mask_format_import_running",
            id=batch.id,
            async_job_id=batch.async_job_id,
        ),
    )
    assert data.mask_import is not None
    return data.mask_import


async def record_import_item_result(
    db: AsyncSession,
    batch: MaskFormatImportSnapshot,
    item_key: str,
    item_result: dict[str, Any],
) -> MaskFormatImportSnapshot:
    data = await _transfer(
        db,
        RecordMaskFormatImportItem(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="record_mask_format_import_item",
            id=batch.id,
            async_job_id=batch.async_job_id,
            item_key=item_key,
            item_result=item_result,
        ),
    )
    assert data.mask_import is not None
    return data.mask_import


async def cancel_import_execution(
    db: AsyncSession, batch: MaskFormatImportSnapshot
) -> int:
    data = await _transfer(
        db,
        FinishMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="finish_mask_format_import",
            action="cancel",
            id=batch.id,
            async_job_id=batch.async_job_id,
            failed=None,
            completed=None,
            skipped=None,
        ),
    )
    assert data.committed_count is not None
    return data.committed_count


async def complete_import_batch(
    db: AsyncSession,
    batch: MaskFormatImportSnapshot,
    *,
    failed: int,
    completed: int,
    skipped: int,
) -> MaskFormatImportSnapshot:
    data = await _transfer(
        db,
        FinishMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="finish_mask_format_import",
            action="complete",
            id=batch.id,
            async_job_id=batch.async_job_id,
            failed=failed,
            completed=completed,
            skipped=skipped,
        ),
    )
    assert data.mask_import is not None
    return data.mask_import


async def fail_import_batch(db: AsyncSession, batch: MaskFormatImportSnapshot) -> None:
    await _transfer(
        db,
        FinishMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="finish_mask_format_import",
            action="fail",
            id=batch.id,
            async_job_id=batch.async_job_id,
            failed=None,
            completed=None,
            skipped=None,
        ),
    )


async def reconcile_job_terminal(db: AsyncSession, terminal: JobTerminal) -> None:
    await _transfer(
        db,
        FinishMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="finish_mask_format_import",
            action="reconcile_cancel" if terminal.cancelled else "reconcile_fail",
            id=None,
            async_job_id=terminal.job_id,
            failed=None,
            completed=None,
            skipped=None,
        ),
    )


async def request_job_cancel(
    db: AsyncSession, job_id: uuid.UUID, *, pending: bool
) -> str:
    batch_data = await _transfer(
        db,
        LockMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_mask_format_import",
            id=None,
            project_id=None,
            async_job_id=job_id,
        ),
    )
    if pending:
        await async_job_svc.mark_cancelled(
            db, job_id, result={"reason": "cancelled_by_user"}
        )
        if (
            batch_data.mask_import is not None
            and batch_data.mask_import.status == "pending"
        ):
            await _transfer(
                db,
                CancelPendingMaskFormatImport(
                    schema=ANNOTATION_TRANSFER_PROTOCOL,
                    operation="cancel_pending_mask_format_import",
                    async_job_id=job_id,
                ),
            )
        await notify_job_terminal(db, job_id=job_id)
        return "cancelled"
    await async_job_svc.request_cancel(db, job_id)
    return "cancel_requested"


async def record_import_dispatch_task(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    async_job_id: uuid.UUID,
    celery_task_id: str,
) -> None:
    data = await _transfer(
        db,
        LockMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="lock_mask_format_import",
            id=batch_id,
            project_id=None,
            async_job_id=async_job_id,
        ),
    )
    if data.mask_import is None:
        return
    job = await async_job_svc.get_job(db, async_job_id)
    if job is not None:
        async_job_svc.bind_execution_id(job, celery_task_id)


async def fail_import_dispatch(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    async_job_id: uuid.UUID,
    error: str,
) -> None:
    data = await _transfer(
        db,
        FinishMaskFormatImport(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="finish_mask_format_import",
            action="fail",
            id=batch_id,
            async_job_id=async_job_id,
            failed=None,
            completed=None,
            skipped=None,
        ),
    )
    if data.mask_import is None:
        return
    await async_job_svc.mark_failed(db, async_job_id, error=error)
    await notify_job_terminal(db, job_id=async_job_id)
