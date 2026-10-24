from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.task_batch import TaskBatch
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.annotation_cleanup import detach_prediction_parents_for_tasks
from app.services.async_job import delete_batch_predict_jobs
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.batch import BatchService
from app.services.prediction import PredictionService
from app.services.project_access import lock_current_project_rows


async def bulk_clear_preannotate(
    db: AsyncSession,
    *,
    batch_ids: list[uuid.UUID],
    mode: Literal["predictions_only", "reset_to_draft"],
    reason: str,
    actor_id: uuid.UUID,
    origin: AuditOriginContext,
) -> dict[str, Any]:
    """Clear preannotation results within the selected mode's lifecycle boundary."""
    batches_result = await db.execute(
        select(TaskBatch).where(TaskBatch.id.in_(batch_ids))
    )
    batches_by_id = {batch.id: batch for batch in batches_result.scalars().all()}

    project_ids = sorted({batch.project_id for batch in batches_by_id.values()})
    projects_by_id: dict[uuid.UUID, Project] = {}
    actor: IdentityActorFacts | IdentityActorStatus | None = None
    for project_id in project_ids:
        try:
            actor, project = await lock_current_project_rows(
                db, actor_id=actor_id, project_id=project_id
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        if actor.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
            raise HTTPException(status_code=403, detail="权限不足")
        projects_by_id[project_id] = project

    batch_service = BatchService(db)
    if actor is None:
        actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
        if actor is None:
            raise HTTPException(status_code=401, detail="账号已停用或不存在")
    is_super = actor.role == UserRole.SUPER_ADMIN
    succeeded: list[uuid.UUID] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    aggregate_cascade = {
        "predictions": 0,
        "failed_predictions": 0,
        "prediction_jobs": 0,
    }

    for batch_id in batch_ids:
        batch = batches_by_id.get(batch_id)
        if batch is None:
            skipped.append({"batch_id": batch_id, "reason": "not_found"})
            continue
        project = projects_by_id.get(batch.project_id)
        if project is None:
            skipped.append({"batch_id": batch_id, "reason": "project_missing"})
            continue
        if not is_super and project.owner_id != actor_id:
            skipped.append({"batch_id": batch_id, "reason": "forbidden"})
            continue

        try:
            if mode == "reset_to_draft":
                _, _, cascade = await batch_service.reset_to_draft(
                    batch_id, actor_id=actor_id
                )
            else:
                await detach_prediction_parents_for_tasks(db, batch_id=batch_id)
                cascade = await PredictionService(db).delete_for_batch(batch_id)
                cascade["prediction_jobs"] = await delete_batch_predict_jobs(
                    db, batch_id
                )
                await batch_service.reactivate_preannotated_after_prediction_clear(
                    batch
                )

            await db.flush()
            for key in aggregate_cascade:
                aggregate_cascade[key] += cascade.get(key, 0)
            succeeded.append(batch_id)
        except Exception as exc:  # noqa: BLE001
            if not db.in_transaction():
                # A domain rollback releases every authority fence and earlier write.
                raise
            failed.append(
                {"batch_id": batch_id, "reason": f"{type(exc).__name__}: {exc}"}
            )

    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.PREANNOTATE_BULK_CLEAR,
        target_type="batch",
        target_id=str(succeeded[0]) if succeeded else "none",
        origin=origin,
        status_code=200,
        detail={
            "mode": mode,
            "reason": reason,
            "requested": len(batch_ids),
            "succeeded_ids": [str(batch_id) for batch_id in succeeded],
            "succeeded": len(succeeded),
            "skipped": len(skipped),
            "failed": len(failed),
            "cascade": aggregate_cascade,
        },
    )
    await db.commit()
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}
