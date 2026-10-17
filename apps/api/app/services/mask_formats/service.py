from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.async_job import AsyncJobKind
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.project import Project
from app.schemas.mask_format import (
    MaskFormatImportBatchOut,
    MaskFormatImportPreflightResponse,
)
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services.mask_formats.contracts import StagedObject, canonical_digest
from app.services.mask_formats.catalog import registry
from app.services.storage import storage_service


RECEIPT_TTL = timedelta(minutes=15)


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


@contextmanager
def materialize_staged_object(
    *,
    object_key: str,
    expected_sha256: str,
) -> Iterator[StagedObject]:
    meta = storage_service.verify_upload(
        object_key,
        bucket=storage_service.import_bucket,
    )
    if meta is None:
        raise MaskFormatError(
            "staged_object_missing",
            "staged object is missing",
            status_code=404,
        )
    content_length = int(meta.get("ContentLength") or 0)
    if content_length > settings.mask_format_temp_quota_bytes:
        raise MaskFormatError(
            "resource_budget_exceeded",
            "staged object exceeds the temporary byte quota",
            status_code=413,
            detail={
                "budget": "mask_format_temp_quota_bytes",
                "limit": settings.mask_format_temp_quota_bytes,
                "observed": content_length,
            },
        )
    with tempfile.TemporaryDirectory(prefix="aap-mask-format-") as temp_dir:
        local_path = Path(temp_dir) / "staged-object"
        response = storage_service.client.get_object(
            Bucket=storage_service.import_bucket,
            Key=object_key,
        )
        body = response["Body"]
        digest = hashlib.sha256()
        written = 0
        try:
            with local_path.open("wb") as sink:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > settings.mask_format_temp_quota_bytes:
                        raise MaskFormatError(
                            "resource_budget_exceeded",
                            "staged object exceeded the streaming byte quota",
                            status_code=413,
                            detail={
                                "budget": "mask_format_temp_quota_bytes",
                                "limit": settings.mask_format_temp_quota_bytes,
                                "observed": written,
                            },
                        )
                    digest.update(chunk)
                    sink.write(chunk)
        finally:
            body.close()
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
        yield StagedObject(
            object_key=object_key,
            sha256=actual_sha256,
            local_path=os.fspath(local_path),
            size_bytes=written,
        )


def batch_out(batch: MaskFormatImport) -> MaskFormatImportBatchOut:
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
    with materialize_staged_object(
        object_key=staged_object_key,
        expected_sha256=staged_sha256,
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

    receipt = f"mfi_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(timezone.utc) + RECEIPT_TTL
    batch = MaskFormatImport(
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
        status="staged",
        result_json={"items": {}},
    )
    db.add(batch)
    await db.flush()
    return MaskFormatImportPreflightResponse(
        import_id=batch.id,
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
) -> tuple[MaskFormatImport, bool]:
    batch = (
        await db.execute(
            select(MaskFormatImport)
            .where(MaskFormatImport.token_hash == _token_hash(receipt))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        batch is None
        or batch.project_id != project.id
        or batch.requested_by_id != user_id
    ):
        raise MaskFormatError(
            "format_receipt_not_found", "format receipt not found", status_code=404
        )
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
    batch.async_job_id = job.id
    batch.status = "pending"
    return batch, True


async def resume_import(
    db: AsyncSession,
    *,
    batch: MaskFormatImport,
    user_id: uuid.UUID,
) -> MaskFormatImport:
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
    batch.async_job_id = job.id
    batch.status = "pending"
    batch.completed_at = None
    return batch


async def dispatch_import(
    batch_id: uuid.UUID, *, dispatcher: QualityJobDispatcher
) -> str:
    try:
        return dispatcher.dispatch_mask_import(batch_id)
    except Exception as exc:
        raise MaskFormatError(
            "format_import_dispatch_failed",
            "format import worker dispatch failed",
            status_code=503,
        ) from exc


def _committed_import_items(batch: MaskFormatImport) -> int:
    return sum(
        1
        for value in ((batch.result_json or {}).get("items") or {}).values()
        if isinstance(value, dict) and value.get("status") == "committed"
    )


async def reconcile_job_terminal(db: AsyncSession, terminal: JobTerminal) -> None:
    """Preserve partial imports when cancellation follows committed items."""
    batch = (
        await db.execute(
            select(MaskFormatImport)
            .where(MaskFormatImport.async_job_id == terminal.job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        return
    if terminal.cancelled:
        if batch.status not in {"pending", "running"}:
            return
        batch.status = "partial" if _committed_import_items(batch) else "cancelled"
    else:
        batch.status = "failed"
    batch.completed_at = datetime.now(timezone.utc)


async def request_job_cancel(
    db: AsyncSession, job_id: uuid.UUID, *, pending: bool
) -> str:
    """Cancel a queued import or request a stop at its next item boundary."""
    batch = (
        await db.execute(
            select(MaskFormatImport)
            .where(MaskFormatImport.async_job_id == job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if pending:
        await async_job_svc.mark_cancelled(
            db, job_id, result={"reason": "cancelled_by_user"}
        )
        if batch is not None and batch.status == "pending":
            batch.status = "cancelled"
            batch.completed_at = datetime.now(timezone.utc)
        await notify_job_terminal(db, job_id=job_id)
        return "cancelled"
    await async_job_svc.request_cancel(db, job_id)
    return "cancel_requested"
