from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import lock_current_task_target
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.labeling_core_read_relations import batch_row, get_core_row
from app.services import task_access
from app.services.annotation_evidence import (
    assert_review_evidence_current,
    review_round_contributor_snapshot,
    task_contributor_snapshot,
)
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.batch import BatchService
from app.services.feedback import FeedbackService
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_tasks import LabelingTaskService
from app.services.notification import NotificationService
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectCapability,
)
from app.services.project_access import assert_capability
from app.services.scheduler import effective_task_reviewer_id


class TaskReviewWorkflow:
    """Compose reviewer transitions with evidence, QC, feedback and delivery."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.labeling = LabelingTaskService(db)

    async def _locked_task(
        self,
        task_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
    ) -> tuple[Task, IdentityActorFacts, Project, ProjectAccess]:
        task, actor, project, access = await lock_current_task_target(
            self.db,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=access,
        )
        assert_capability(access, ProjectCapability.REVIEW_WRITE.value)
        await task_access.assert_task_visible(self.db, task, actor.id, access=access)
        return task, actor, project, access

    async def _assert_review_evidence_allowed(
        self, *, task: Task, actor: IdentityActorFacts
    ) -> None:
        effective_annotator_id = await task_access.resolve_effective_task_assignee_id(
            self.db, task
        )
        assert_review_evidence_current(
            task, actor.id, effective_annotator_id=effective_annotator_id
        )

    async def _assert_review_owner(
        self,
        *,
        task: Task,
        actor: IdentityActorFacts,
        access: ProjectAccess,
        project: Project,
    ) -> Project:
        task_access.assert_access_binding(
            access, actor_id=actor.id, project_id=task.project_id
        )
        if access.is_manager:
            return project
        batch = (
            await get_core_row(self.db, batch_row, task.batch_id)
            if task.batch_id
            else None
        )
        effective_reviewer_id = effective_task_reviewer_id(task, batch)
        if effective_reviewer_id != actor.id or task.reviewer_claimed_at is None:
            raise TaskWorkflowError(
                409,
                {
                    "reason": "task_review_not_claimed_by_user",
                    "reviewer_id": (
                        str(effective_reviewer_id) if effective_reviewer_id else None
                    ),
                },
            )
        return project

    @staticmethod
    def _assert_review_state(task: Task) -> None:
        if task.status != "review":
            raise TaskWorkflowError(
                409, {"reason": "task_not_in_review", "status": task.status}
            )

    async def claim_review(
        self,
        task_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
        origin: AuditOriginContext,
    ) -> dict[str, Any]:
        task, actor, _project, access = await self._locked_task(
            task_id, actor_id=actor_id, access=access
        )
        self._assert_review_state(task)
        await self._assert_review_evidence_allowed(task=task, actor=actor)

        if task.reviewer_claimed_at is None:
            batch = (
                await get_core_row(self.db, batch_row, task.batch_id)
                if task.batch_id
                else None
            )
            effective_reviewer_id = effective_task_reviewer_id(task, batch)
            if (
                effective_reviewer_id is not None
                and effective_reviewer_id != actor.id
                and not access.is_manager
            ):
                raise TaskWorkflowError(
                    409,
                    {
                        "reason": "task_review_assigned_to_other",
                        "reviewer_id": str(effective_reviewer_id),
                    },
                )
            review_round_id = await self.labeling.claim_review(
                task, reviewer_id=actor.id, claimed_at=datetime.now(timezone.utc)
            )
            await AuditService.log(
                self.db,
                actor=actor,
                action=AuditAction.TASK_REVIEW_CLAIM,
                target_type="task",
                target_id=str(task_id),
                origin=origin,
                status_code=200,
                detail={
                    "project_id": str(task.project_id),
                    "review_round_id": str(review_round_id),
                    "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                    "contributor_ids": await task_contributor_snapshot(self.db, task),
                },
            )
            await self.db.commit()

        return {
            "task_id": task.id,
            "reviewer_id": task.reviewer_id,
            "reviewer_claimed_at": task.reviewer_claimed_at,
            "is_self": task.reviewer_id == actor.id,
        }

    async def approve_task(
        self,
        task_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
        origin: AuditOriginContext,
        expected_qc_digest: str | None,
        warning_issue_ids: list[uuid.UUID],
        note: str | None,
    ) -> dict[str, str]:
        task, actor, project, access = await self._locked_task(
            task_id, actor_id=actor_id, access=access
        )
        self._assert_review_state(task)
        await self._assert_review_evidence_allowed(task=task, actor=actor)
        await self._assert_review_owner(
            task=task, actor=actor, access=access, project=project
        )

        from app.services.video_canonical import (
            VideoBoundaryUnreconciledError,
            assert_task_boundaries_reconciled,
        )

        try:
            await assert_task_boundaries_reconciled(self.db, project=project, task=task)
        except VideoBoundaryUnreconciledError as exc:
            raise TaskWorkflowError(409, exc.detail) from exc

        from app.services.mask_qc.config import load_mask_qc_config
        from app.services.mask_qc.service import (
            current_completed_task_run,
            current_open_task_issues,
            qc_digest_for_issues,
        )

        config = load_mask_qc_config(project.mask_qc_config)
        completed_run, source_digest = await current_completed_task_run(
            self.db, task=task, project=project
        )
        qc_digest = (
            await qc_digest_for_issues(self.db, run=completed_run, task_id=task.id)
            if completed_run is not None
            else None
        )
        if expected_qc_digest is not None and qc_digest != expected_qc_digest:
            raise TaskWorkflowError(
                409,
                {
                    "reason": "mask_qc_digest_conflict",
                    "expected": expected_qc_digest,
                    "actual": qc_digest,
                },
            )
        if config.enabled and config.blocking and source_digest is not None:
            if completed_run is None:
                raise TaskWorkflowError(409, {"reason": "mask_qc_current_run_required"})
            blockers = await current_open_task_issues(
                self.db, task_id=task.id, severity="blocker"
            )
            if blockers:
                raise TaskWorkflowError(
                    409,
                    {
                        "reason": "mask_qc_blockers_present",
                        "issue_ids": [str(issue.id) for issue in blockers],
                        "qc_digest": qc_digest,
                    },
                )

        warning_ids = set(warning_issue_ids)
        if warning_ids:
            warnings = await current_open_task_issues(
                self.db,
                task_id=task.id,
                severity="warning",
                issue_ids=warning_ids,
            )
            if {issue.id for issue in warnings} != warning_ids:
                raise TaskWorkflowError(
                    409, {"reason": "mask_qc_warning_issue_invalid"}
                )

        now = datetime.now(timezone.utc)
        review_round_id = await self.labeling.approve_review(
            task, reviewer_id=actor.id, reviewed_at=now
        )
        contributor_ids = await review_round_contributor_snapshot(self.db, task)
        await self.labeling.record_first_review_fact(
            task,
            actor_id=actor.id,
            reviewed_at=now,
            result="approved",
            contributor_ids=contributor_ids,
        )
        await self.labeling.record_project_review_approval(project)

        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_APPROVE,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "contributor_ids": contributor_ids,
                "review_round_id": str(review_round_id),
                "result": "approved",
                "mask_qc_digest": qc_digest,
                "mask_qc_warning_issue_ids": sorted(
                    str(value) for value in warning_ids
                ),
                "mask_qc_note": note.strip() if note else None,
            },
        )

        pending_notifications: list[Notification] = []
        if task.assignee_id is not None and task.assignee_id != actor.id:
            pending_notifications = await NotificationService(self.db).notify_many(
                user_ids=[task.assignee_id],
                type="task.approved",
                target_type="task",
                target_id=task.id,
                payload={
                    "task_display_id": task.display_id,
                    "project_id": str(task.project_id),
                    "actor_id": str(actor.id),
                    "actor_name": actor.name,
                },
            )

        await self.db.commit()
        if pending_notifications:
            await NotificationService(self.db).publish_committed(pending_notifications)
        return {"status": "approved", "task_id": str(task_id)}

    async def reject_task(
        self,
        task_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        access: ProjectAccess,
        origin: AuditOriginContext,
        reason_type: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        task, actor, project, access = await self._locked_task(
            task_id, actor_id=actor_id, access=access
        )
        self._assert_review_state(task)
        await self._assert_review_evidence_allowed(task=task, actor=actor)
        await self._assert_review_owner(
            task=task, actor=actor, access=access, project=project
        )
        if reason_type is None:
            raise TaskWorkflowError(422, "reject reason_type is required")

        reason_text = reason.strip() if reason else None
        now = datetime.now(timezone.utc)
        review_round_id = await self.labeling.reject_review(
            task,
            reviewer_id=actor.id,
            reviewed_at=now,
            reason_type=reason_type,
            reason=reason_text,
        )
        contributor_ids = await review_round_contributor_snapshot(self.db, task)
        await self.labeling.record_first_review_fact(
            task,
            actor_id=actor.id,
            reviewed_at=now,
            result="rejected",
            contributor_ids=contributor_ids,
        )
        rejected_review_contributor_ids = task.review_contributor_ids
        rejected_review_submitter_id = task.review_submitter_id
        await self.labeling.clear_review_contributor_evidence(task, actor_id=actor.id)
        await self.db.flush()
        await FeedbackService(self.db).mirror_task_reject(task, reviewer_id=actor.id)

        await self.labeling.decrement_project_review_count(project)
        batch_service = BatchService(self.db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.TASK_REJECT,
            target_type="task",
            target_id=str(task_id),
            origin=origin,
            status_code=200,
            detail={
                "project_id": str(task.project_id),
                "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                "reason_type": task.reject_reason_type,
                "reason": task.reject_reason,
                "contributor_ids": contributor_ids,
                "review_contributor_ids": rejected_review_contributor_ids,
                "review_submitter_id": (
                    str(rejected_review_submitter_id)
                    if rejected_review_submitter_id
                    else None
                ),
                "review_round_id": str(review_round_id),
                "result": "rejected",
            },
        )

        pending_notifications: list[Notification] = []
        if task.assignee_id is not None and task.assignee_id != actor.id:
            pending_notifications = await NotificationService(self.db).notify_many(
                user_ids=[task.assignee_id],
                type="task.rejected",
                target_type="task",
                target_id=task.id,
                payload={
                    "task_display_id": task.display_id,
                    "project_id": str(task.project_id),
                    "reject_reason_type": task.reject_reason_type,
                    "reject_reason": task.reject_reason,
                    "actor_id": str(actor.id),
                    "actor_name": actor.name,
                },
            )

        await self.db.commit()
        if pending_notifications:
            await NotificationService(self.db).publish_committed(pending_notifications)
        return {
            "status": "rejected",
            "task_id": str(task_id),
            "reason_type": task.reject_reason_type,
            "reason": task.reject_reason,
        }
