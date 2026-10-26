from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_labeling.batch_control_contract import (
    ApplyBatchAssignments,
    ApplyTaskAssignments,
    AssignTasks,
    BatchControlCommand,
    BatchControlFailure,
    BatchControlResult,
    BatchControlUnavailable,
    BulkApproveBatches,
    BulkBatchStatus,
    BulkDeleteBatches,
    BulkRejectBatches,
    CheckAutoTransition,
    ClearAnnotationContributors,
    ClearPredictionProjection,
    DeactivatePredictionAnnotations,
    DetachPredictionParents,
    MarkPreannotated,
    PrepareBatchCleanup,
    ReactivatePreannotated,
    RecalculateBatchCounters,
    RejectBatch,
    ReassignBatchTasks,
    ResetBatchToDraft,
    SetBatchAdminLock,
    SetSequenceOrders,
    TransitionBatch,
    UpdateBatch,
)
from aap_labeling.batch_control_data import LabelingBatchControlData
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock

_BATCH_CONTROL_SQL = text(
    "SELECT aap_labeling_v1.batch_control_command(CAST(:request AS jsonb))"
)


def _task_control_cache_fields(request: BatchControlCommand) -> set[str]:
    fields: set[str] = set()
    if isinstance(request, UpdateBatch):
        if request.annotator_set:
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        if request.reviewer_set:
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
    elif isinstance(request, PrepareBatchCleanup):
        pass
    elif isinstance(request, (ReassignBatchTasks, AssignTasks, BulkDeleteBatches)):
        fields.update(("batch_id", "updated_at"))
    elif isinstance(request, SetSequenceOrders):
        fields.update(("sequence_order", "updated_at"))
    elif isinstance(request, ApplyBatchAssignments):
        if any(item.annotator_set for item in request.assignments):
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        if any(item.reviewer_set for item in request.assignments):
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
    elif isinstance(request, ApplyTaskAssignments):
        if any(item.annotator_set for item in request.assignments):
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        reviewer_items = [item for item in request.assignments if item.reviewer_set]
        if reviewer_items:
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
            if any(
                item.expected_reviewer_id != item.reviewer_id for item in reviewer_items
            ):
                fields.add("reviewer_claimed_at")
    elif isinstance(request, (RejectBatch, BulkRejectBatches)):
        fields.update(
            ("status", "review_contributor_ids", "review_submitter_id", "updated_at")
        )
        if isinstance(request, BulkRejectBatches):
            fields.add("is_labeled")
    elif isinstance(request, ClearAnnotationContributors):
        fields.update(("annotation_contributor_ids", "updated_at"))
    elif isinstance(request, ClearPredictionProjection):
        fields.update(
            ("total_predictions", "total_annotations", "is_labeled", "updated_at")
        )
    return fields


def _task_control_cache_fields_for_row(
    request: BatchControlCommand, task: Task, result: BatchControlResult
) -> set[str]:
    if isinstance(request, UpdateBatch):
        fields: set[str] = set()
        if request.annotator_set and (
            not task.assignee_is_override or task.assignee_id is None
        ):
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        if request.reviewer_set and (
            not task.reviewer_is_override or task.reviewer_id is None
        ):
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
        return fields
    if isinstance(request, ApplyBatchAssignments):
        assignment = next(
            (item for item in request.assignments if item.batch_id == task.batch_id),
            None,
        )
        if assignment is None:
            return set()
        fields = set()
        if assignment.annotator_set and (
            not task.assignee_is_override or task.assignee_id is None
        ):
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        if assignment.reviewer_set and (
            not task.reviewer_is_override or task.reviewer_id is None
        ):
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
        return fields
    if isinstance(request, ApplyTaskAssignments):
        assignment = next(
            (item for item in request.assignments if item.task_id == task.id),
            None,
        )
        if assignment is None:
            return set()
        fields = set()
        if assignment.annotator_set:
            fields.update(
                ("assignee_id", "assignee_is_override", "assigned_at", "updated_at")
            )
        if assignment.reviewer_set:
            fields.update(("reviewer_id", "reviewer_is_override", "updated_at"))
            if assignment.expected_reviewer_id != assignment.reviewer_id:
                fields.add("reviewer_claimed_at")
        return fields
    if isinstance(request, PrepareBatchCleanup):
        fields = set()
        if task.status != "pending":
            fields.update(
                (
                    "status",
                    "review_contributor_ids",
                    "review_submitter_id",
                    "updated_at",
                )
            )
        if task.id in result.cleared_assignee_task_ids:
            fields.update(("assignee_id", "updated_at"))
        if task.id in result.cleared_reviewer_task_ids:
            fields.update(("reviewer_id", "reviewer_claimed_at", "updated_at"))
        return fields
    if isinstance(request, (RejectBatch, BulkRejectBatches)) and task.status not in {
        "review",
        "completed",
    }:
        return set()
    if isinstance(request, AssignTasks) and (
        task.id not in request.task_ids or task.project_id != result.project_id
    ):
        return set()
    if isinstance(request, ReassignBatchTasks) and task.batch_id != request.batch_id:
        return set()
    if isinstance(request, BulkDeleteBatches) and task.batch_id not in {
        item.batch_id for item in request.batches
    }:
        return set()
    if isinstance(request, SetSequenceOrders) and task.id not in {
        item.task_id for item in request.sequence_orders
    }:
        return set()
    if isinstance(request, ApplyTaskAssignments) and task.id not in {
        item.task_id for item in request.assignments
    }:
        return set()
    if (
        isinstance(request, ClearAnnotationContributors)
        and task.id not in request.task_ids
    ):
        return set()
    if (
        isinstance(request, ClearPredictionProjection)
        and task.id not in request.task_ids
    ):
        return set()
    return _task_control_cache_fields(request)


def _batch_control_cache_fields(request: BatchControlCommand) -> set[str]:
    if isinstance(request, UpdateBatch):
        fields: set[str] = set()
        for selected, names in (
            (request.name_set, ("name",)),
            (request.description_set, ("description",)),
            (request.priority_set, ("priority",)),
            (request.deadline_set, ("deadline",)),
            (request.annotator_set, ("annotator_id", "assigned_user_ids")),
            (request.reviewer_set, ("reviewer_id", "assigned_user_ids")),
        ):
            if selected:
                fields.update(names)
        if fields:
            fields.add("updated_at")
        return fields
    if isinstance(request, TransitionBatch):
        fields = {"status", "updated_at"}
        if (
            request.expected_status == "approved"
            and request.target_status == "reviewing"
        ):
            fields.update(("review_feedback", "reviewed_at", "reviewed_by"))
        return fields
    if isinstance(request, SetBatchAdminLock):
        return {
            "admin_locked",
            "admin_lock_reason",
            "admin_locked_at",
            "admin_locked_by",
            "updated_at",
        }
    if isinstance(request, ResetBatchToDraft):
        return {"status", "review_feedback", "reviewed_at", "reviewed_by", "updated_at"}
    if isinstance(request, ApplyBatchAssignments):
        fields = {"assigned_user_ids", "updated_at"}
        if any(item.annotator_set for item in request.assignments):
            fields.add("annotator_id")
        if any(item.reviewer_set for item in request.assignments):
            fields.add("reviewer_id")
        return fields
    if isinstance(request, BulkBatchStatus):
        return {"status", "updated_at"}
    if isinstance(request, BulkApproveBatches):
        return {"status", "updated_at"}
    if isinstance(request, BulkRejectBatches):
        return {
            "status",
            "review_feedback",
            "reviewed_at",
            "reviewed_by",
            "total_tasks",
            "completed_tasks",
            "review_tasks",
            "updated_at",
        }
    if isinstance(request, RejectBatch):
        return {"status", "review_feedback", "reviewed_at", "reviewed_by", "updated_at"}
    if isinstance(request, RecalculateBatchCounters):
        return {"total_tasks", "completed_tasks", "review_tasks", "updated_at"}
    if isinstance(
        request, (MarkPreannotated, CheckAutoTransition, ReactivatePreannotated)
    ):
        return {"status", "updated_at"}
    return set()


async def execute_batch_control(
    db: AsyncSession, request: BatchControlCommand
) -> BatchControlResult:
    """Run a fixed Labeling command in the caller's transaction and refresh tracked rows."""
    if db.autoflush:
        await db.flush()
    try:
        result = await LabelingBatchControlData(
            lambda request_json: db.scalar(
                _BATCH_CONTROL_SQL, {"request": request_json}
            )
        ).command(request)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise HTTPException(409, detail={"reason": "batch_control_busy"}) from exc
        raise
    except BatchControlUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "batch_control_unavailable"}
        ) from exc
    except BatchControlFailure as exc:
        details = dict(exc.details)
        if exc.code == "batch_not_found":
            raise HTTPException(404, detail="Batch not found") from exc
        if exc.code == "default_batch_protected":
            raise HTTPException(400, detail="Cannot delete the default batch") from exc
        if exc.code == "batch_tasks_not_reassigned":
            raise HTTPException(
                409, detail={"reason": "batch_tasks_not_reassigned"}
            ) from exc
        if exc.code == "batch_delete_protection_changed":
            raise HTTPException(
                409,
                detail={
                    "code": "batch_has_active_work",
                    "requires_force": True,
                    "non_pending": int(details.get("non_pending", 0)),
                    "predicted": int(details.get("predicted", 0)),
                    "affected_tasks": int(details.get("affected_tasks", 0)),
                    "message": (
                        "批次含进行中/已完成或 AI 预标过的任务；删除将重置为待标注"
                        "并清除 AI 预标。请改用归档，或确认强制删除。"
                    ),
                },
            ) from exc
        if exc.code == "batch_transition_invalid":
            raise HTTPException(
                400,
                detail=(
                    f"Cannot transition from '{details.get('from_status')}' "
                    f"to '{details.get('target_status')}'"
                ),
            ) from exc
        if exc.code == "batch_empty":
            raise HTTPException(400, detail="cannot activate empty batch") from exc
        if exc.code == "batch_already_locked":
            raise HTTPException(409, detail="Batch is already locked") from exc
        if exc.code == "batch_not_locked":
            raise HTTPException(409, detail="Batch is not locked") from exc
        raise HTTPException(409, detail={"reason": exc.code, **details}) from exc

    tracked = {
        task.id: task for task in db.identity_map.values() if isinstance(task, Task)
    }
    if result.changed:
        for state in result.tasks:
            task = tracked.get(state.id)
            if task is None:
                continue
            fields = _task_control_cache_fields_for_row(request, task, result)
            for field in fields:
                set_committed_value(task, field, getattr(state, field))
    annotation_fields: tuple[str, ...] = ()
    if isinstance(request, DeactivatePredictionAnnotations):
        annotation_fields = ("is_active", "updated_at")
    elif isinstance(request, DetachPredictionParents):
        annotation_fields = ("parent_prediction_id", "updated_at")
    annotations = {
        annotation.id: annotation
        for annotation in db.identity_map.values()
        if isinstance(annotation, Annotation)
    }
    for state in result.annotations:
        annotation = annotations.get(state.id)
        if annotation is not None:
            for field in annotation_fields:
                set_committed_value(annotation, field, getattr(state, field))
    batch_fields = _batch_control_cache_fields(request)
    if result.changed and batch_fields:
        batches = {
            row.id: row
            for row in db.identity_map.values()
            if isinstance(row, TaskBatch)
        }
        for state in result.batches:
            batch = batches.get(state.id)
            if batch is not None:
                for field in batch_fields:
                    set_committed_value(batch, field, getattr(state, field))
    deleted_task_ids = (
        set(result.task_ids) if isinstance(request, PrepareBatchCleanup) else set()
    )
    if result.deleted_batch_ids or deleted_task_ids:
        deleted_batches = set(result.deleted_batch_ids)
        for row in list(db.identity_map.values()):
            if (
                isinstance(row, TaskBatch)
                and row.id in deleted_batches
                or isinstance(row, TaskLock)
                and row.task_id in deleted_task_ids
            ):
                db.sync_session.expunge(row)
    return result
