from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.project import Project
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.mask_formats import registry
from app.services.mask_formats.contracts import canonical_digest
from app.services.mask_formats.service import materialize_staged_object
from app.workers.celery_app import celery_app


def _result_copy(batch: MaskFormatImport) -> dict[str, Any]:
    result = dict(batch.result_json or {})
    result["items"] = dict(result.get("items") or {})
    return result


async def _cancel_if_requested(
    db: AsyncSession,
    *,
    batch: MaskFormatImport,
    job: AsyncJob,
) -> bool:
    if job.status != "cancelled" and not bool(
        (job.payload or {}).get("cancel_requested")
    ):
        return False
    committed = sum(
        1
        for value in _result_copy(batch)["items"].values()
        if isinstance(value, dict) and value.get("status") == "committed"
    )
    batch.status = "partial" if committed else "cancelled"
    batch.completed_at = datetime.now(timezone.utc)
    await async_job_svc.mark_cancelled(
        db,
        job.id,
        result={"committed": committed, "reason": "cancelled_by_user"},
    )
    await notify_job_terminal(db, job_id=job.id)
    await db.commit()
    return True


async def _run_mask_format_import(
    batch_id: str,
    celery_task_id: str | None,
) -> None:
    import_id = uuid.UUID(batch_id)
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskFormatImport)
                    .where(MaskFormatImport.id == import_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is None or batch.async_job_id is None:
                raise RuntimeError("mask format import batch not found")
            job = await db.get(AsyncJob, batch.async_job_id)
            if job is None:
                raise RuntimeError("mask format import async job not found")
            if batch.status not in {"pending", "running"}:
                return
            if await _cancel_if_requested(db, batch=batch, job=job):
                return
            batch.status = "running"
            await async_job_svc.mark_running(
                db,
                job.id,
                celery_task_id=celery_task_id,
            )
            await db.commit()
            job_id = job.id
            format_id = batch.format_id
            staged_object_key = batch.staged_object_key
            staging_ref = deepcopy(batch.staging_ref)
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

        async with (
            SessionLocal() as reading,
            materialize_staged_object(
                reading,
                object_key=staged_object_key,
                expected_sha256=staged_sha256,
                staging_ref=staging_ref,
            ) as staged,
        ):
            async with SessionLocal() as db:
                batch = await db.get(MaskFormatImport, import_id)
                if batch is None:
                    raise RuntimeError("mask format import batch disappeared")
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
            from app.schemas.mask_format import MaskFormatPlan

            typed_plan = MaskFormatPlan.model_validate(plan)
            completed = failed = skipped = 0
            total = len(typed_plan.items)
            for item_index, _item in enumerate(typed_plan.items):
                key = str(item_index)
                async with SessionLocal() as db:
                    batch = (
                        await db.execute(
                            select(MaskFormatImport)
                            .where(MaskFormatImport.id == import_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    job = await db.get(AsyncJob, job_id)
                    if job is None:
                        raise RuntimeError("mask format import async job disappeared")
                    if await _cancel_if_requested(db, batch=batch, job=job):
                        return
                    existing = _result_copy(batch)["items"].get(key)
                    if isinstance(existing, dict) and existing.get("status") in {
                        "committed",
                        "skipped",
                    }:
                        if existing["status"] == "committed":
                            completed += 1
                        else:
                            skipped += 1
                        continue
                    project = await db.get(Project, project_id)
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
                        result = _result_copy(batch)
                        result["items"][key] = item_result
                        batch.result_json = result
                        await db.commit()
                        if item_result.get("status") == "committed":
                            completed += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        await db.rollback()
                        batch = (
                            await db.execute(
                                select(MaskFormatImport)
                                .where(MaskFormatImport.id == import_id)
                                .with_for_update()
                            )
                        ).scalar_one()
                        result = _result_copy(batch)
                        result["items"][key] = {
                            "status": "failed",
                            "reason": str(exc)[:500],
                        }
                        batch.result_json = result
                        await db.commit()
                        failed += 1
                async with SessionLocal() as db:
                    await async_job_svc.update_progress(
                        db,
                        job_id,
                        int(((item_index + 1) / max(1, total)) * 100),
                    )
                    await db.commit()

        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskFormatImport)
                    .where(MaskFormatImport.id == import_id)
                    .with_for_update()
                )
            ).scalar_one()
            batch.status = (
                "partial"
                if failed and (completed or skipped)
                else ("failed" if failed else "completed")
            )
            batch.completed_at = datetime.now(timezone.utc)
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
        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskFormatImport)
                    .where(MaskFormatImport.id == import_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is not None:
                batch.status = "failed"
                batch.completed_at = datetime.now(timezone.utc)
                if batch.async_job_id is not None:
                    await async_job_svc.mark_failed(
                        db,
                        batch.async_job_id,
                        error=str(exc),
                    )
                    await notify_job_terminal(db, job_id=batch.async_job_id)
                await db.commit()
        raise
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True, name="app.workers.mask_format_import.run_mask_format_import"
)
def run_mask_format_import(self, batch_id: str) -> None:
    asyncio.run(
        _run_mask_format_import(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
        )
    )
