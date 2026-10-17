from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services import task_access
from app.services.annotation_evidence import (
    capture_first_review_contributor_snapshot,
    clear_review_contributor_evidence,
    ensure_review_round,
    freeze_review_contributor_evidence,
    task_contributor_snapshot,
)
from app.services.audit import AuditAction, AuditOriginContext, AuditService
from app.services.batch import BatchService
from app.services.labeling_tasks import LabelingTaskService
from app.services.notification import NotificationService
from app.services.project_access import ProjectAccess


class TaskLifecycleWorkflow:
    """Compose labeling lifecycle transitions with their transactional effects."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.labeling = LabelingTaskService(db)

    async def skip_task(
        self,
        task_id: uuid.UUID,
        *,
        reason: str,
        note: str | None,
        actor: User,
        access: ProjectAccess,
        origin: AuditOriginContext,
    ) -> dict[str, Any]:
        task = (
            await self.db.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise TaskWorkflowError(404, "Task not found")
        await task_access.assert_task_visible(self.db, task, actor, access=access)
        task_access.assert_effective_task_assignee(
            actor,
            await task_access.resolve_effective_task_assignee_id(self.db, task),
            action="skip",
            project_id=task.project_id,
            allow_open_pool=True,
            access=access,
        )
        if task.status not in ("pending", "in_progress"):
            raise TaskWorkflowError(
                409,
                {"reason": "task_not_skippable", "status": task.status},
            )
        task_access.assert_task_editable(task, actor, access=access)

        now = datetime.now(timezone.utc)
        review_round_id, inherited_assignee_id = await self.labeling.skip_task(
            task, reason=reason, actor_id=actor.id, now=now
        )
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)

        contributor_ids = await task_contributor_snapshot(self.db, task)
        first_review_contributors = list(contributor_ids)

        capture_first_review_contributor_snapshot(task, contributor_ids)
        if (
            inherited_assignee_id is not None
            and str(inherited_assignee_id) not in first_review_contributors
        ):
            first_review_contributors.append(str(inherited_assignee_id))
        freeze_review_contributor_evidence(
            task,
            submitter_id=actor.id,
            contributor_ids=first_review_contributors,
        )
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_SKIP,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "skip_reason": reason,
                "note": note,
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "contributor_ids": contributor_ids,
                "review_contributor_ids": task.review_contributor_ids,
                "review_submitter_id": (
                    str(task.review_submitter_id) if task.review_submitter_id else None
                ),
                "review_round_id": str(review_round_id),
                "result": "skipped",
            },
        )
        await self.db.commit()
        return {"status": "skipped", "task_id": str(task_id), "skip_reason": reason}

    async def withdraw_task(
        self,
        task_id: uuid.UUID,
        *,
        actor: User,
        access: ProjectAccess,
        origin: AuditOriginContext,
    ) -> dict[str, str]:
        task = await self.db.get(Task, task_id)
        if task is None:
            raise TaskWorkflowError(404, "Task not found")
        project = await self.db.get(Project, task.project_id)
        if project is None:
            raise TaskWorkflowError(404, "Task not found")
        task_access.assert_effective_task_assignee(
            actor,
            await task_access.resolve_effective_task_assignee_id(self.db, task),
            action="withdraw",
            project_id=task.project_id,
            access=access,
        )
        if task.status != "review":
            raise TaskWorkflowError(
                409, {"reason": "task_not_in_review", "status": task.status}
            )
        if task.reviewer_claimed_at is not None:
            raise TaskWorkflowError(
                409,
                {
                    "reason": "task_already_claimed",
                    "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
                },
            )

        self.labeling.withdraw_task(task)
        clear_review_contributor_evidence(task)
        self.labeling.decrement_project_review_count(project)
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_WITHDRAW,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={"project_id": str(task.project_id)},
        )
        await self.db.commit()
        return {"status": "withdrawn", "task_id": str(task_id)}

    async def reopen_task(
        self,
        task_id: uuid.UUID,
        *,
        actor: User,
        access: ProjectAccess,
        origin: AuditOriginContext,
    ) -> dict[str, Any]:
        task = await self.db.get(Task, task_id)
        if task is None:
            raise TaskWorkflowError(404, "Task not found")
        project = await self.db.get(Project, task.project_id)
        if project is None:
            raise TaskWorkflowError(404, "Task not found")
        task_access.assert_effective_task_assignee(
            actor,
            await task_access.resolve_effective_task_assignee_id(self.db, task),
            action="reopen",
            project_id=task.project_id,
            access=access,
        )
        if task.status != "completed":
            raise TaskWorkflowError(
                409, {"reason": "task_not_completed", "status": task.status}
            )

        original_reviewer_id = self.labeling.reopen_task(
            task, now=datetime.now(timezone.utc)
        )
        clear_review_contributor_evidence(task)
        self.labeling.decrement_project_completed_count(project)
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_REOPEN,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "original_reviewer_id": str(original_reviewer_id)
                if original_reviewer_id
                else None,
                "contributor_ids": await task_contributor_snapshot(self.db, task),
                "review_round_id": str(ensure_review_round(task)),
                "reopened_count": task.reopened_count,
            },
        )

        pending_notifications = []
        if original_reviewer_id is not None:
            pending_notifications = await NotificationService(self.db).notify_many(
                user_ids=[original_reviewer_id],
                type="task.reopened",
                target_type="task",
                target_id=task.id,
                payload={
                    "task_display_id": task.display_id,
                    "project_id": str(task.project_id),
                    "actor_id": str(actor.id),
                    "actor_name": actor.name,
                    "reopened_count": task.reopened_count,
                },
            )

        await self.db.commit()
        if pending_notifications:
            await NotificationService(self.db).publish_committed(pending_notifications)
        return {
            "status": "reopened",
            "task_id": str(task_id),
            "reopened_count": task.reopened_count,
        }

    async def accept_rejection(
        self,
        task_id: uuid.UUID,
        *,
        actor: User,
        access: ProjectAccess,
        origin: AuditOriginContext,
    ) -> dict[str, str]:
        task = await self.db.get(Task, task_id)
        if task is None:
            raise TaskWorkflowError(404, "Task not found")
        project = await self.db.get(Project, task.project_id)
        if project is None:
            raise TaskWorkflowError(404, "Task not found")
        task_access.assert_effective_task_assignee(
            actor,
            await task_access.resolve_effective_task_assignee_id(self.db, task),
            action="accept rejection",
            project_id=task.project_id,
            access=access,
        )
        if task.status != "rejected":
            raise TaskWorkflowError(
                409, {"reason": "task_not_rejected", "status": task.status}
            )

        self.labeling.accept_rejection(task)
        clear_review_contributor_evidence(task)
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_ACCEPT_REJECTION,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "reject_reason": task.reject_reason,
                "review_round_id": str(ensure_review_round(task)),
                "contributor_ids": await task_contributor_snapshot(self.db, task),
            },
        )
        await self.db.commit()
        return {"status": "in_progress", "task_id": str(task_id)}
