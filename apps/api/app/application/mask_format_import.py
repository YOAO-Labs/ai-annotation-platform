"""Application workflow for staged mask-format import execution."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.services.project_access import get_workspace_project
from app.schemas.mask_format import MaskFormatPlan
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.mask_formats.catalog import registry
from app.services.mask_formats.contracts import canonical_digest
from app.services.mask_formats.service import (
    cancel_import_execution,
    complete_import_batch,
    fail_import_batch,
    lock_import,
    mark_import_running,
    materialize_staged_object,
    read_import,
    record_import_item_result,
)


async def run_mask_format_import(
    batch_id: str,
    celery_task_id: str | None,
    *,
    session_factory: Any,
) -> None:
    """Execute durable import items with one transaction and journal per item."""
    import_id = uuid.UUID(batch_id)

    async def cancel_if_requested(db: AsyncSession, *, batch, job: AsyncJob) -> bool:
        if job.status != "cancelled" and not bool(
            (job.payload or {}).get("cancel_requested")
        ):
            return False
        committed = await cancel_import_execution(db, batch)
        await async_job_svc.mark_cancelled(
            db,
            job.id,
            result={"committed": committed, "reason": "cancelled_by_user"},
        )
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        return True

    try:
        async with session_factory() as db:
            batch = await lock_import(db, import_id=import_id)
            if batch is None or batch.async_job_id is None:
                raise RuntimeError("mask format import batch not found")
            job = await db.get(AsyncJob, batch.async_job_id)
            if job is None:
                raise RuntimeError("mask format import async job not found")
            if (
                celery_task_id
                and job.celery_task_id
                and job.celery_task_id != celery_task_id
            ):
                return
            if batch.status not in {"pending", "running"}:
                return
            if await cancel_if_requested(db, batch=batch, job=job):
                return
            batch = await mark_import_running(db, batch)
            await async_job_svc.mark_running(
                db,
                job.id,
                celery_task_id=celery_task_id,
            )
            await db.commit()
            job_id = job.id
            format_id = batch.format_id
            staged_object_key = batch.staged_object_key
            staged_sha256 = batch.staged_sha256
            adapter_version = batch.adapter_version
            manifest_version = batch.manifest_version
            mapping_digest = batch.mapping_digest
            options_digest = batch.options_digest
            plan_digest = batch.plan_digest

        adapter = registry.get(format_id)
        if (
            adapter.descriptor.adapter_version != adapter_version
            or adapter.descriptor.manifest_version != manifest_version
        ):
            raise RuntimeError("format_adapter_version_conflict")

        with materialize_staged_object(
            object_key=staged_object_key,
            expected_sha256=staged_sha256,
        ) as staged:
            async with session_factory() as db:
                batch = await read_import(db, import_id=import_id)
                if batch is None:
                    raise RuntimeError("mask format import batch disappeared")
                if batch.async_job_id != job_id:
                    return
                plan_json = dict(batch.plan_json or {})
                if (
                    canonical_digest(
                        {
                            key: value
                            for key, value in plan_json.items()
                            if key != "plan_digest"
                        }
                    )
                    != plan_digest
                ):
                    raise RuntimeError("format_plan_digest_conflict")
                if canonical_digest(batch.mapping_json or {}) != mapping_digest:
                    raise RuntimeError("format_mapping_digest_conflict")
                if canonical_digest(batch.options_json or {}) != options_digest:
                    raise RuntimeError("format_options_digest_conflict")
                plan = batch.plan_json
                options = dict(batch.options_json or {})
                mapping = dict(batch.mapping_json or {})
                requested_by_id = batch.requested_by_id
                project_id = batch.project_id

            typed_plan = MaskFormatPlan.model_validate(plan)
            completed = failed = skipped = 0
            total = len(typed_plan.items)
            for item_index, _item in enumerate(typed_plan.items):
                key = str(item_index)
                async with session_factory() as db:
                    batch = await lock_import(db, import_id=import_id)
                    if batch is None:
                        raise RuntimeError("mask format import batch disappeared")
                    if batch.async_job_id != job_id:
                        return
                    job = await db.get(AsyncJob, job_id)
                    if job is None:
                        raise RuntimeError("mask format import async job disappeared")
                    if await cancel_if_requested(db, batch=batch, job=job):
                        return
                    existing = (batch.result_json or {}).get("items", {}).get(key)
                    if isinstance(existing, dict) and existing.get("status") in {
                        "committed",
                        "skipped",
                    }:
                        if existing["status"] == "committed":
                            completed += 1
                        else:
                            skipped += 1
                        continue
                    project = await get_workspace_project(db, project_id=project_id)
                    if project is None or requested_by_id is None:
                        raise RuntimeError("format import project or actor missing")
                    try:
                        item_result = await adapter.execute_import_item(
                            db,
                            project=project,
                            staged=staged,
                            plan=typed_plan,
                            item_index=item_index,
                            operator_user_id=requested_by_id,
                            mapping=mapping,
                            options=options,
                        )
                        await record_import_item_result(db, batch, key, item_result)
                        await db.commit()
                        if item_result.get("status") == "committed":
                            completed += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        await db.rollback()
                        batch = await lock_import(db, import_id=import_id)
                        if batch is None:
                            raise RuntimeError("mask format import batch disappeared")
                        if batch.async_job_id != job_id:
                            return
                        await record_import_item_result(
                            db,
                            batch,
                            key,
                            {
                                "status": "failed",
                                "reason": str(exc)[:500],
                            },
                        )
                        await db.commit()
                        failed += 1
                async with session_factory() as db:
                    await async_job_svc.update_progress(
                        db,
                        job_id,
                        int(((item_index + 1) / max(1, total)) * 100),
                    )
                    await db.commit()

        async with session_factory() as db:
            batch = await lock_import(db, import_id=import_id)
            if batch is None:
                raise RuntimeError("mask format import batch disappeared")
            if batch.async_job_id != job_id:
                return
            batch = await complete_import_batch(
                db,
                batch,
                failed=failed,
                completed=completed,
                skipped=skipped,
            )
            summary = {
                "mask_format_import_id": str(batch.id),
                "success_count": completed,
                "failed_count": failed,
                "skipped_count": skipped,
                "result_digest": canonical_digest(batch.result_json or {}),
            }
            if batch.status == "failed":
                await async_job_svc.mark_failed(
                    db,
                    job_id,
                    error="all selected format import items failed",
                    result=summary,
                )
            else:
                await async_job_svc.mark_complete(db, job_id, result=summary)
            await notify_job_terminal(db, job_id=job_id)
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            batch = await lock_import(db, import_id=import_id)
            if batch is not None and (
                "job_id" not in locals() or batch.async_job_id == job_id
            ):
                await fail_import_batch(db, batch)
                if batch.async_job_id is not None:
                    await async_job_svc.mark_failed(
                        db,
                        batch.async_job_id,
                        error=str(exc),
                    )
                    await notify_job_terminal(db, job_id=batch.async_job_id)
                await db.commit()
        raise
