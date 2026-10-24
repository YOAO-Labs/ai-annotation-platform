"""Local final-write admission for immutable inference-to-labeling facts."""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_video_inputs.evidence import VideoEvidenceFailure, VideoInputEvidence
from aap_assets_video_inputs.execution import (
    FrozenFramePlan,
    LockVideoInputRequest,
    VideoInputFailure,
)
from aap_assets_video_inputs.values import VIDEO_INPUT_SCHEMA

from app.db.enums import WORKBENCH_AI_EDITABLE_TASK_STATUSES
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJobKind
from app.services.jobs_lifecycle_data import job_lock_statement
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import task_access
from app.services.batch_permissions import allows_bulk_preannotation
from app.services.labeling_write_scope import (
    LabelingBatchWriteScope,
    LabelingWriteOperation,
    LabelingWriteScope,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.project_access import lock_current_project_access
from app.services.task_lock import TaskLockService

_MANAGER_OPERATIONS = frozenset(
    {"preannotation", "prediction_import", "prediction_retry"}
)
_JOB_KINDS = {
    "preannotation": "batch_predict",
    "prediction_import": "predictions_import",
    "prediction_retry": "prediction_retry",
    "mask_repair": AsyncJobKind.MASK_REPAIR.value,
}


async def _lock_accepted_input_source(
    db: AsyncSession,
    scope: LabelingWriteScope,
    *,
    expected_job_id: uuid.UUID | None = None,
) -> None:
    """Run the source-only native fence inside this Labeling transaction."""
    from aap_assets_inputs.contract import InputFailure

    from app.services.accepted_inputs import lock_input_source

    try:
        record = await lock_input_source(
            db,
            input_id=scope.accepted_input_id,
            task_id=scope.task_id,
            project_id=scope.project_id,
        )
        if expected_job_id is not None and record.job_id != expected_job_id:
            raise _conflict("prediction_input_manifest_origin_mismatch")
    except InputFailure as exc:
        if exc.code in {"input_busy", "source_busy", "reference_busy", "job_busy"}:
            raise _conflict("input_busy") from exc
        if exc.code == "source_changed":
            raise _conflict("prediction_source_changed") from exc
        raise HTTPException(
            status_code=409,
            detail={"reason": "accepted_input_unavailable", "code": exc.code},
        ) from exc


async def _lock_accepted_video_input_pin(
    db: AsyncSession,
    scope: LabelingWriteScope,
    evidence: VideoInputEvidence,
) -> None:
    """Pin the exact retained frame inside the final Labeling transaction."""
    if type(evidence) is not VideoInputEvidence:
        raise _conflict("accepted_video_input_unavailable")
    if (
        evidence.task_id != scope.task_id
        or evidence.project_id != scope.project_id
        or scope.job_id is None
        or scope.accepted_input_id is not None
        or scope.operation not in {"preannotation", "prediction_retry"}
    ):
        raise _conflict("prediction_source_changed")
    from app.services.accepted_video_inputs import lock_video_input_pin

    try:
        await lock_video_input_pin(
            db,
            task_id=scope.task_id,
            project_id=scope.project_id,
            evidence=evidence,
            actor_id=scope.actor_id,
        )
    except VideoEvidenceFailure as exc:
        if exc.code in {
            "input_busy",
            "source_busy",
            "reference_busy",
            "manifest_busy",
            "frame_busy",
            "job_busy",
        }:
            raise _conflict("input_busy") from exc
        if exc.code in {"source_changed", "task_scope_changed", "input_scope_mismatch"}:
            raise _conflict("prediction_source_changed") from exc
        raise HTTPException(
            status_code=409,
            detail={"reason": "accepted_video_input_unavailable", "code": exc.code},
        ) from exc


async def _lock_accepted_video_input_source(
    db: AsyncSession,
    scope: LabelingWriteScope,
    plan: FrozenFramePlan,
) -> None:
    """Fence overwrite or failure accounting against the retained original."""
    if type(plan) is not FrozenFramePlan:
        raise _conflict("accepted_video_input_unavailable")
    frozen = plan.input
    if (
        scope.actor_id is None
        or frozen.job_id != scope.job_id
        or frozen.task.task_id != scope.task_id
        or frozen.task.project_id != scope.project_id
        or frozen.task.task_source_generation != scope.task_source_generation
        or frozen.task.dataset_item_id != scope.source_dataset_item_id
        or frozen.task.file_path != scope.source_file_path
        or frozen.task.file_type != scope.source_file_type
    ):
        raise _conflict("prediction_source_changed")
    from app.services.accepted_video_inputs import lock_video_input_source

    try:
        record = await lock_video_input_source(
            db,
            LockVideoInputRequest(
                input_id=frozen.input_id,
                job_id=frozen.job_id,
                task_id=frozen.task.task_id,
                actor_id=scope.actor_id,
                plan_digest=plan.plan_digest,
            ),
        )
    except VideoInputFailure as exc:
        if exc.code in {
            "input_busy",
            "source_busy",
            "reference_busy",
            "manifest_busy",
            "job_busy",
        }:
            raise _conflict("input_busy") from exc
        if exc.code in {"source_changed", "task_scope_changed"}:
            raise _conflict("prediction_source_changed") from exc
        raise HTTPException(
            status_code=409,
            detail={"reason": "accepted_video_input_unavailable", "code": exc.code},
        ) from exc
    if record.plan != plan:
        raise _conflict("prediction_source_changed")


def _conflict(reason: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"reason": reason})


async def _locked_row(db: AsyncSession, statement, *, reason: str):
    try:
        return (await db.execute(statement)).scalar_one_or_none()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise _conflict(reason) from exc
        raise


async def _assert_execution_current(
    db: AsyncSession,
    scope: LabelingWriteScope | LabelingBatchWriteScope,
    *,
    task_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
    accepted_input_job_id: uuid.UUID | None = None,
    accepted_video_input: VideoInputEvidence | None = None,
    accepted_video_plan: FrozenFramePlan | None = None,
) -> None:
    if scope.job_id is None:
        return
    job = await _locked_row(
        db,
        job_lock_statement(scope.job_id, mode="share_nowait", populate_existing=True),
        reason="job_busy",
    )
    if (
        job is None
        or job.project_id != scope.project_id
        or job.user_id != scope.actor_id
        or job.kind != _JOB_KINDS.get(scope.operation)
    ):
        raise _conflict("prediction_execution_scope_mismatch")
    payload = job.payload or {}
    # A running item's cooperative cancellation is observed at its existing
    # item boundary; an actual terminal job can never admit a late result.
    if job.status not in {"pending", "running"} or (
        job.status == "pending" and payload.get("cancel_requested")
    ):
        raise _conflict("prediction_execution_not_writable")
    if scope.operation != "preannotation":
        return
    if "batch_id" not in payload or "execution_scope" not in payload:
        raise _conflict("labeling_write_scope_unknown")
    if payload["execution_scope"] != scope.execution_scope:
        raise _conflict("prediction_execution_scope_mismatch")
    saved_batch = payload["batch_id"]
    if saved_batch is not None and str(saved_batch) != str(batch_id):
        raise _conflict("prediction_execution_scope_mismatch")
    if task_id is None and saved_batch is None:
        raise _conflict("prediction_execution_scope_mismatch")
    selected = payload.get("task_ids")
    if selected is not None and (
        not isinstance(selected, list)
        or (task_id is not None and str(task_id) not in selected)
    ):
        raise _conflict("prediction_execution_scope_mismatch")
    manifest_protocol = payload.get("accepted_input_manifest_protocol")
    if manifest_protocol == "aap-assets-input-manifest/v1" and task_id is not None:
        if (
            accepted_input_job_id != job.id
            or scope.job_id != job.id
            or scope.accepted_input_id is None
        ):
            raise _conflict("prediction_input_manifest_origin_mismatch")
    elif accepted_input_job_id is not None:
        raise _conflict("prediction_input_manifest_origin_mismatch")
    video_protocol = payload.get("accepted_video_inputs_protocol")
    video_payload_fields = {
        "accepted_video_manifest_id",
        "accepted_video_selection_digest",
        "accepted_video_selection",
    }
    if video_protocol is None:
        if (
            video_payload_fields.intersection(payload)
            or accepted_video_input is not None
            or accepted_video_plan is not None
        ):
            raise _conflict("prediction_video_manifest_origin_mismatch")
        return
    if (
        video_protocol != VIDEO_INPUT_SCHEMA
        or payload.get("accepted_video_manifest_id") != str(job.id)
        or not isinstance(payload.get("accepted_video_selection"), dict)
        or not isinstance(payload.get("accepted_video_selection_digest"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", payload.get("accepted_video_selection_digest", "")
        )
        is None
        or payload.get("accepted_inputs_protocol") is not None
        or payload.get("accepted_input_manifest_protocol") is not None
        or accepted_input_job_id is not None
        or (accepted_video_input is None) == (accepted_video_plan is None)
        or task_id is None
    ):
        raise _conflict("prediction_video_manifest_origin_mismatch")
    if accepted_video_input is not None and (
        type(accepted_video_input) is not VideoInputEvidence
        or accepted_video_input.job_id != job.id
        or accepted_video_input.task_id != task_id
        or accepted_video_input.project_id != scope.project_id
    ):
        raise _conflict("prediction_video_manifest_origin_mismatch")
    if accepted_video_plan is not None and (
        type(accepted_video_plan) is not FrozenFramePlan
        or accepted_video_plan.input.job_id != job.id
        or accepted_video_plan.input.task.task_id != task_id
        or accepted_video_plan.input.task.project_id != scope.project_id
    ):
        raise _conflict("prediction_video_manifest_origin_mismatch")


async def lock_labeling_write(
    db: AsyncSession,
    scope: LabelingWriteScope,
    *,
    operations: tuple[LabelingWriteOperation, ...],
    accepted_input_job_id: uuid.UUID | None = None,
    accepted_video_input: VideoInputEvidence | None = None,
    accepted_video_plan: FrozenFramePlan | None = None,
) -> tuple[Task, User, ProjectAccess]:
    """Hold current authority and target locks through the caller's transaction.

    A captured scope is a precondition, never a grant. Known jobless commands
    remain jobless; unknown actors/scopes never become system authorization.
    """
    if scope.operation not in operations:
        raise _conflict("labeling_write_scope_mismatch")
    if accepted_input_job_id is not None and (
        scope.operation != "preannotation"
        or scope.job_id != accepted_input_job_id
        or scope.accepted_input_id is None
    ):
        raise _conflict("prediction_input_manifest_origin_mismatch")
    if accepted_video_input is not None and (
        type(accepted_video_input) is not VideoInputEvidence
        or accepted_video_input.task_id != scope.task_id
        or accepted_video_input.project_id != scope.project_id
        or scope.accepted_input_id is not None
        or scope.job_id is None
        or scope.operation not in {"preannotation", "prediction_retry"}
    ):
        raise _conflict("accepted_video_input_unavailable")
    if accepted_video_plan is not None and (
        type(accepted_video_plan) is not FrozenFramePlan
        or scope.operation != "preannotation"
        or accepted_video_input is not None
        or accepted_input_job_id is not None
        or scope.accepted_input_id is not None
        or scope.job_id is None
    ):
        raise _conflict("accepted_video_input_unavailable")
    if not scope.actor_known or scope.actor_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if (
        not scope.execution_scope_known
        or not scope.expected_batch_id_known
        or not scope.job_id_known
        or scope.task_version is None
        or not scope.source_binding_known
        or type(scope.task_source_generation) is not int
        or scope.task_source_generation <= 0
        or scope.source_file_path is None
        or scope.source_file_type is None
    ):
        raise _conflict("labeling_write_scope_unknown")
    with db.no_autoflush:
        actor, _project, access = await lock_current_project_access(
            db, actor_id=scope.actor_id, project_id=scope.project_id
        )
        if scope.operation in _MANAGER_OPERATIONS and not access.is_manager:
            raise HTTPException(status_code=403, detail="项目管理权限已变化")
        batch = None
        if scope.expected_batch_id is not None:
            batch = await _locked_row(
                db,
                select(TaskBatch)
                .where(
                    TaskBatch.id == scope.expected_batch_id,
                    TaskBatch.project_id == scope.project_id,
                )
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True),
                reason="batch_busy",
            )
            if batch is None:
                raise _conflict("prediction_task_scope_changed")
        task = await _locked_row(
            db,
            select(Task)
            .where(Task.id == scope.task_id, Task.project_id == scope.project_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True),
            reason="task_busy",
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if (
            task.version != scope.task_version
            or task.status != scope.task_status
            or task.assignee_id != scope.assignee_id
            or task.review_round_id != scope.review_round_id
            or task.batch_id != scope.expected_batch_id
        ):
            raise _conflict("prediction_task_scope_changed")
        if (
            task.media_source_generation != scope.task_source_generation
            or task.dataset_item_id != scope.source_dataset_item_id
            or task.file_path != scope.source_file_path
            or task.file_type != scope.source_file_type
        ):
            raise _conflict("prediction_source_changed")
        await task_access.assert_task_visible(db, task, actor.id, access=access)
        if scope.operation == "preannotation":
            if scope.execution_scope not in {"bulk", "workbench"}:
                raise _conflict("labeling_write_scope_unknown")
            if batch is not None and batch.admin_locked:
                raise _conflict("batch_admin_locked")
            if scope.execution_scope == "bulk":
                if task.status != "pending" or (
                    batch is not None and not allows_bulk_preannotation(batch)
                ):
                    raise _conflict("preannotation_task_not_eligible")
            elif task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES:
                raise _conflict("preannotation_task_not_eligible")
        elif scope.operation not in {"prediction_import", "prediction_retry"}:
            task_access.assert_annotation_write_allowed(
                task,
                access,
                allow_review_adjustment=scope.operation != "secondary_inference",
            )
            task_access.assert_task_editable(task, actor.id, access=access)
            await task_access.assert_review_adjustment_evidence(
                db, task, actor.id, access
            )
            if task.status != "review":
                task_access.assert_effective_task_assignee(
                    actor.id,
                    await task_access.resolve_effective_task_assignee_id(db, task),
                    action="write",
                    project_id=task.project_id,
                    allow_open_pool=True,
                    access=access,
                )
        if scope.operation != "prediction_import":
            await TaskLockService(db).assert_final_write_available(
                task.id,
                actor.id,
                allow_own=not (
                    scope.operation == "preannotation"
                    and scope.execution_scope == "bulk"
                ),
            )
        if scope.operation in {"secondary_inference", "mask_repair"}:
            if (
                scope.source_annotation_id is None
                or scope.source_annotation_version is None
            ):
                raise _conflict("labeling_write_scope_unknown")
            source = await _locked_row(
                db,
                select(Annotation)
                .where(
                    Annotation.id == scope.source_annotation_id,
                    Annotation.task_id == task.id,
                    Annotation.project_id == task.project_id,
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True),
                reason="annotation_busy",
            )
            if source is None or not source.is_active or source.was_cancelled:
                raise HTTPException(status_code=404, detail="annotation not found")
            if source.version != scope.source_annotation_version:
                raise _conflict("annotation_version_conflict")
        await _assert_execution_current(
            db,
            scope,
            task_id=task.id,
            batch_id=task.batch_id,
            accepted_input_job_id=accepted_input_job_id,
            accepted_video_input=accepted_video_input,
            accepted_video_plan=accepted_video_plan,
        )
        if scope.accepted_input_id is not None:
            await _lock_accepted_input_source(
                db,
                scope,
                expected_job_id=(
                    accepted_input_job_id
                    if scope.operation == "preannotation"
                    else None
                ),
            )
        if accepted_video_input is not None:
            await _lock_accepted_video_input_pin(db, scope, accepted_video_input)
        if accepted_video_plan is not None:
            await _lock_accepted_video_input_source(db, scope, accepted_video_plan)
    return task, actor, access


async def lock_labeling_batch_write(
    db: AsyncSession, scope: LabelingBatchWriteScope
) -> TaskBatch:
    """Revalidate a batch prediction's current authority before lifecycle advance."""
    if scope.operation != "preannotation":
        raise _conflict("labeling_write_scope_mismatch")
    if not scope.actor_known or scope.actor_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if (
        not scope.execution_scope_known
        or not scope.job_id_known
        or scope.execution_scope not in {"bulk", "workbench"}
    ):
        raise _conflict("labeling_write_scope_unknown")
    with db.no_autoflush:
        _actor, _project, access = await lock_current_project_access(
            db, actor_id=scope.actor_id, project_id=scope.project_id
        )
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="项目管理权限已变化")
        batch = await _locked_row(
            db,
            select(TaskBatch)
            .where(
                TaskBatch.id == scope.batch_id,
                TaskBatch.project_id == scope.project_id,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True),
            reason="batch_busy",
        )
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        await _assert_execution_current(db, scope, task_id=None, batch_id=batch.id)
    return batch
