from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import lock_core_nowait
from app.db.enums import PlatformRole, ProjectRole
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.point_cloud_quality import PointCloudQualityIssue
from app.db.models.task import Task
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorFacts
from app.schemas.annotation_feedback import (
    AnnotationFeedbackCreate,
    AnnotationFeedbackPatch,
    AnnotationFeedbackReply,
    serialize_feedback_anchor,
)
from app.services import task_access
from app.services.annotation_comment import AnnotationCommentService
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.discussion_actions import discussion_actions
from app.services.discussion_notifications import (
    prepare_feedback_comment_mention_notifications,
    prepare_feedback_reply_notifications,
    prepare_feedback_status_notifications,
)
from app.services.feedback import FeedbackService
from app.services.mask_qc.service import effective_issue_status, load_mask_qc_issue
from app.services.notification import NotificationService
from app.services.point_cloud_quality.service import refresh_issue_staleness
from app.services.labeling_spatial_quality import quality_row, spatial_quality_data
from aap_workspace_contract.access import ProjectAccess
from app.services.project_access import (
    lock_current_project_access,
    resolve_project_access_by_id,
)
from app.services.labeling_core_read_relations import (
    annotation_row,
    execute_core_read,
    get_core_row,
    lock_annotation_rows_for_core_read,
    lock_batch_rows_for_core_read,
    lock_task_for_core_read,
    task_row,
)

logger = logging.getLogger(__name__)
_FEEDBACK_PLATFORM_ROLES = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)
_FEEDBACK_UNAVAILABLE = "feedback thread is unavailable"
_FEEDBACK_NOT_FOUND = "feedback not found"


@dataclass(frozen=True, slots=True)
class _FeedbackSnapshot:
    id: uuid.UUID
    kind: str
    anchor_type: str
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    annotation_id: uuid.UUID | None
    anchor_position: dict[str, Any] | None
    thread_parent_id: uuid.UUID | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class FeedbackMutationResult:
    entry: AnnotationFeedback
    project_role: str | None
    is_manager: bool
    can_reply: bool = False


class AnnotationFeedbackWorkflow:
    """Own admission, authorization, and transactional effects for feedback writes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.feedback = FeedbackService(db)

    @staticmethod
    def _assert_platform_role(actor: IdentityActorFacts) -> None:
        if actor.role not in _FEEDBACK_PLATFORM_ROLES:
            roles = "或".join(_FEEDBACK_PLATFORM_ROLES)
            raise TaskWorkflowError(403, f"需要角色权限: {roles}")

    async def create(
        self,
        payload: AnnotationFeedbackCreate,
        *,
        actor_id: uuid.UUID,
        origin: AuditOriginContext,
    ) -> FeedbackMutationResult:
        actor, _project, access = await lock_current_project_access(
            self.db, actor_id=actor_id, project_id=payload.project_id
        )
        self._assert_platform_role(actor)
        task = await self._lock_task_scope(
            payload.project_id,
            payload.task_id,
            actor=actor,
            access=access,
            missing_detail="Task not found",
        )
        annotation = await self._lock_annotation(
            payload.annotation_id,
            project_id=payload.project_id,
            task_id=payload.task_id,
            creating=True,
        )
        self._assert_canvas_drawing_scope(payload, task)
        if (
            payload.kind == "issue"
            and payload.thread_parent_id is None
            and task is not None
            and task.file_type == "image"
            and annotation is not None
            and (not annotation.is_active or bool(annotation.was_cancelled))
        ):
            raise TaskWorkflowError(422, {"reason": "feedback_annotation_unavailable"})
        if payload.mentions:
            await AnnotationCommentService(self.db).validate_mentions(
                payload.project_id, [mention.user_id for mention in payload.mentions]
            )

        root: AnnotationFeedback | None = None
        if payload.thread_parent_id is not None:
            if payload.kind != "comment":
                raise TaskWorkflowError(422, "feedback replies must use kind=comment")
            if payload.severity is not None or payload.title is not None:
                raise TaskWorkflowError(
                    422, "feedback replies cannot set severity or title"
                )
            parent_snapshots = await self._read_feedback_lineage(
                payload.thread_parent_id,
                missing_detail=_FEEDBACK_NOT_FOUND,
            )
            await self._lock_feedback_lineage(parent_snapshots)
            parent, root = await self.feedback.validate_parent(
                payload.thread_parent_id,
                project_id=payload.project_id,
                task_id=payload.task_id,
                annotation_id=payload.annotation_id,
                anchor_type=payload.anchor_type,
                anchor_position=serialize_feedback_anchor(payload),
            )
            if root.kind == "comment" and root.anchor_type == "annotation":
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
            if not payload.attachments and not payload.body.strip():
                raise TaskWorkflowError(422, "feedback replies must contain text")

        anchor = payload.anchor_position
        if anchor and anchor.mask_qc_issue_id:
            issue = await self._lock_mask_qc_issue(anchor.mask_qc_issue_id)
            if issue is None:
                raise TaskWorkflowError(404, "Mask QC issue not found")
            expected_bbox = issue.region_bbox
            expected_bbox_values = (
                [
                    expected_bbox["x0"],
                    expected_bbox["y0"],
                    expected_bbox["x1"],
                    expected_bbox["y1"],
                ]
                if expected_bbox is not None
                else None
            )
            actual_bbox = list(anchor.region_bbox) if anchor.region_bbox else None
            if (
                issue.project_id != payload.project_id
                or issue.task_id != payload.task_id
                or issue.annotation_id != payload.annotation_id
                or issue.frame_start != anchor.frame
                or issue.region_digest != anchor.region_digest
                or actual_bbox != expected_bbox_values
            ):
                raise TaskWorkflowError(
                    409, {"reason": "mask_qc_feedback_anchor_conflict"}
                )
            if await effective_issue_status(self.db, issue) == "stale":
                raise TaskWorkflowError(409, {"reason": "mask_qc_issue_stale"})
        if anchor and anchor.point_cloud_quality_issue_id:
            issue = await self._lock_point_cloud_issue(
                anchor.point_cloud_quality_issue_id
            )
            if issue is None or issue.task_id is None:
                raise TaskWorkflowError(404, "3D Quality issue not found")
            issue_task = task
            if issue_task is None or issue_task.id != issue.task_id:
                issue_task = await self._read_task_fresh(issue.task_id)
                if issue_task is None:
                    raise TaskWorkflowError(404, "3D Quality issue not found")
                _, issue_access = await resolve_project_access_by_id(
                    self.db, actor_id=actor.id, project_id=issue_task.project_id
                )
                await task_access.assert_task_visible(
                    self.db, issue_task, actor.id, access=issue_access
                )
            locator = issue.locator or {}
            if (
                issue.project_id != payload.project_id
                or issue.task_id != payload.task_id
                or issue.annotation_id != payload.annotation_id
                or issue.scene_id != anchor.scene_id
                or issue.scene_track_id != anchor.scene_track_id
                or issue.frame_start != anchor.frame
                or locator.get("auxiliary_layers") != anchor.auxiliary_layers
            ):
                raise TaskWorkflowError(
                    409,
                    {"reason": "point_cloud_quality_feedback_anchor_conflict"},
                )
            if await refresh_issue_staleness(self.db, issue):
                raise TaskWorkflowError(
                    409, {"reason": "point_cloud_quality_issue_stale"}
                )

        entry = await self.feedback.create(
            author_id=actor.id,
            kind=payload.kind,
            anchor_type=payload.anchor_type,
            project_id=payload.project_id,
            task_id=payload.task_id,
            annotation_id=payload.annotation_id,
            anchor_position=serialize_feedback_anchor(payload),
            severity=payload.severity,
            title=payload.title,
            body=payload.body,
            attachments=payload.attachments,
            mentions=[
                mention.model_dump(by_alias=True, mode="json")
                for mention in payload.mentions
            ],
            thread_parent_id=payload.thread_parent_id,
            canvas_drawing=(
                payload.canvas_drawing.model_dump(mode="json")
                if payload.canvas_drawing is not None
                else None
            ),
        )
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.FEEDBACK_CREATED,
            target_type="feedback",
            target_id=entry.id,
            origin=origin,
            status_code=200,
            detail={
                "kind": entry.kind,
                "anchor_type": entry.anchor_type,
                "project_id": str(entry.project_id),
                "task_id": str(entry.task_id) if entry.task_id else None,
                "annotation_id": (
                    str(entry.annotation_id) if entry.annotation_id else None
                ),
                "mention_count": len(payload.mentions),
            },
        )
        pending_notifications = []
        if task is not None:
            pending_notifications = (
                await prepare_feedback_comment_mention_notifications(
                    self.db,
                    comment=entry,
                    task=task,
                    actor=actor,
                    mentioned_user_ids=[
                        mention.user_id for mention in payload.mentions
                    ],
                )
            )
        if root is not None:
            pending_notifications.extend(
                await prepare_feedback_reply_notifications(
                    self.db,
                    root=root,
                    reply=entry,
                    actor=actor,
                )
            )
        await self.db.commit()
        await NotificationService(self.db).publish_committed(pending_notifications)
        entry = await self.feedback.refresh(entry)
        _project, output_access = await resolve_project_access_by_id(
            self.db, actor_id=actor.id, project_id=entry.project_id
        )
        return FeedbackMutationResult(
            entry=entry,
            project_role=output_access.project_role,
            is_manager=output_access.is_manager,
            can_reply=(
                entry.kind == "issue" and await self.quality_anchor_is_current(entry)
            ),
        )

    async def patch(
        self,
        feedback_id: uuid.UUID,
        payload: AnnotationFeedbackPatch,
        *,
        actor_id: uuid.UUID,
        origin: AuditOriginContext,
    ) -> FeedbackMutationResult:
        actor, access, entry, root, _task = await self._admit_feedback_target(
            feedback_id, actor_id=actor_id
        )
        is_author = entry.author_id == actor.id
        is_admin = access.is_manager
        is_reviewer = access.project_role == ProjectRole.REVIEWER.value
        fields = payload.model_fields_set
        forbidden_reviewer_fields = fields & {"severity", "title", "body", "mentions"}
        reviewer_status_only = is_reviewer and not (is_author or is_admin)
        if reviewer_status_only and forbidden_reviewer_fields:
            raise TaskWorkflowError(403, "reviewers may update Issue status only")
        capabilities = discussion_actions(
            "feedback",
            entry.kind,
            is_author=is_author,
            is_admin=is_admin,
            is_reviewer=is_reviewer,
            is_accessible=True,
            can_reply=False,
        )
        wants_status = "status" in fields and payload.status is not None
        wants_content = bool(fields & {"severity", "title", "body", "mentions"})
        if reviewer_status_only and not wants_status:
            raise TaskWorkflowError(403, "reviewers may update Issue status only")
        if (wants_status and not capabilities.change_status) or (
            wants_content and not capabilities.edit
        ):
            raise TaskWorkflowError(403, "not allowed")
        if payload.mentions is not None:
            if not (
                entry.kind == "comment"
                and entry.anchor_type == "task"
                and entry.thread_parent_id is None
            ):
                raise TaskWorkflowError(
                    422, "mentions require a native root task comment"
                )
            await AnnotationCommentService(self.db).validate_mentions(
                root.project_id, [mention.user_id for mention in payload.mentions]
            )
        if (
            payload.body is not None
            and not entry.attachments
            and not (entry.canvas_drawing or {}).get("shapes")
            and not payload.body.strip()
            and (
                entry.thread_parent_id is not None
                or (entry.kind == "comment" and entry.anchor_type == "task")
            )
        ):
            raise TaskWorkflowError(422, "feedback replies must contain text")

        old_status = entry.status
        updated = await self.feedback.patch(
            feedback_id,
            actor_id=actor.id,
            status=payload.status,
            severity=payload.severity,
            title=payload.title,
            body=payload.body,
            mentions=(
                [
                    mention.model_dump(by_alias=True, mode="json")
                    for mention in payload.mentions
                ]
                if payload.mentions is not None
                else None
            ),
        )
        if payload.status is not None and payload.status != old_status:
            await AuditService.log(
                self.db,
                actor=actor,
                action=AuditAction.FEEDBACK_STATUS_CHANGED,
                target_type="feedback",
                target_id=feedback_id,
                origin=origin,
                status_code=200,
                detail={"from": old_status, "to": payload.status},
            )
        pending_notifications = []
        if (
            payload.status is not None
            and payload.status != old_status
            and entry.id == root.id
            and root.kind == "issue"
        ):
            pending_notifications = await prepare_feedback_status_notifications(
                self.db,
                root=root,
                actor=actor,
                from_status=old_status,
                to_status=payload.status,
            )
        await self.db.commit()
        await NotificationService(self.db).publish_committed(pending_notifications)
        updated = await self.feedback.refresh(updated)
        return FeedbackMutationResult(
            entry=updated,
            project_role=access.project_role,
            is_manager=access.is_manager,
            can_reply=(
                root.kind == "issue" and await self.quality_anchor_is_current(root)
            ),
        )

    async def soft_delete(
        self,
        feedback_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        origin: AuditOriginContext,
    ) -> None:
        actor, access, entry, _root, _task = await self._admit_feedback_target(
            feedback_id, actor_id=actor_id
        )
        capabilities = discussion_actions(
            "feedback",
            entry.kind,
            is_author=entry.author_id == actor.id,
            is_admin=access.is_manager,
            is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
            is_accessible=True,
            can_reply=False,
        )
        if not capabilities.delete:
            raise TaskWorkflowError(403, "not allowed")
        await self.feedback.soft_delete(feedback_id)
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.FEEDBACK_DELETED,
            target_type="feedback",
            target_id=feedback_id,
            origin=origin,
            status_code=204,
            detail=None,
        )
        await self.db.commit()

    async def reply(
        self,
        feedback_id: uuid.UUID,
        payload: AnnotationFeedbackReply,
        *,
        actor_id: uuid.UUID,
        origin: AuditOriginContext,
    ) -> FeedbackMutationResult:
        actor, _access, parent, root, task = await self._admit_feedback_target(
            feedback_id, actor_id=actor_id
        )
        if not parent.is_active:
            raise TaskWorkflowError(404, _FEEDBACK_NOT_FOUND)
        if root.kind == "comment" and root.anchor_type == "annotation":
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        await self._assert_reply_anchor_is_current(parent, actor=actor, task=task)
        if not payload.attachments and not payload.body.strip():
            raise TaskWorkflowError(422, "feedback replies must contain text")
        reply = await self.feedback.create(
            author_id=actor.id,
            kind="comment",
            anchor_type=parent.anchor_type,
            project_id=parent.project_id,
            task_id=parent.task_id,
            annotation_id=parent.annotation_id,
            anchor_position=parent.anchor_position,
            severity=None,
            title=None,
            body=payload.body,
            attachments=payload.attachments,
            thread_parent_id=parent.id,
        )
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.FEEDBACK_CREATED,
            target_type="feedback",
            target_id=reply.id,
            origin=origin,
            status_code=200,
            detail={"reply_to": str(parent.id)},
        )
        pending_notifications = await prepare_feedback_reply_notifications(
            self.db,
            root=root,
            reply=reply,
            actor=actor,
        )
        await self.db.commit()
        await NotificationService(self.db).publish_committed(pending_notifications)
        reply = await self.feedback.refresh(reply)
        _project, output_access = await resolve_project_access_by_id(
            self.db, actor_id=actor.id, project_id=root.project_id
        )
        return FeedbackMutationResult(
            entry=reply,
            project_role=output_access.project_role,
            is_manager=output_access.is_manager,
        )

    async def assert_feedback_scope(
        self, entry_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> AnnotationFeedback:
        """Recheck current project/task/annotation ancestry for a feedback read."""
        snapshots = await self._read_feedback_lineage(
            entry_id, missing_detail=_FEEDBACK_NOT_FOUND
        )
        root_snapshot = snapshots[-1]
        actor = await IdentityAccountLockData(self.db).read_actor_status(actor_id)
        if actor is None:
            raise TaskWorkflowError(401, "Authentication required")
        _project, access = await resolve_project_access_by_id(
            self.db, actor_id=actor.id, project_id=root_snapshot.project_id
        )
        task = None
        if root_snapshot.task_id is not None:
            task = await self._read_task_fresh(root_snapshot.task_id)
            if task is None or task.project_id != root_snapshot.project_id:
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
            await task_access.assert_task_visible(
                self.db, task, actor.id, access=access
            )
        await self._read_annotation_scope(
            root_snapshot.annotation_id,
            project_id=root_snapshot.project_id,
            task_id=root_snapshot.task_id,
        )
        root = await self.feedback.get(root_snapshot.id, populate_existing=True)
        if root is None:
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        return root

    async def quality_anchor_is_current(self, entry: AnnotationFeedback) -> bool:
        """Preserve the existing read-side QC freshness policy for UI actions."""
        anchor = entry.anchor_position or {}
        mask_qc_issue_id = anchor.get("mask_qc_issue_id")
        if mask_qc_issue_id:
            try:
                issue_id = uuid.UUID(str(mask_qc_issue_id))
            except (TypeError, ValueError):
                return False
            issue = await load_mask_qc_issue(self.db, issue_id=issue_id)
            if issue is None:
                return False
            return await effective_issue_status(self.db, issue) != "stale"
        point_cloud_issue_id = anchor.get("point_cloud_quality_issue_id")
        if point_cloud_issue_id:
            try:
                issue_id = uuid.UUID(str(point_cloud_issue_id))
            except (TypeError, ValueError):
                return False
            data = await spatial_quality_data(
                self.db, "get_point_issue", issue_id=issue_id
            )
            issue = (
                quality_row(PointCloudQualityIssue, data["row"])
                if data["row"]
                else None
            )
            if issue is None:
                return False
            try:
                return not await refresh_issue_staleness(self.db, issue)
            except Exception:
                logger.exception("unable to validate point-cloud feedback anchor")
                return False
        return True

    async def _admit_feedback_target(
        self, feedback_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> tuple[
        IdentityActorFacts,
        ProjectAccess,
        AnnotationFeedback,
        AnnotationFeedback,
        Task | None,
    ]:
        snapshots = await self._read_feedback_lineage(
            feedback_id, missing_detail=_FEEDBACK_NOT_FOUND
        )
        root_snapshot = snapshots[-1]
        actor, _project, access = await lock_current_project_access(
            self.db, actor_id=actor_id, project_id=root_snapshot.project_id
        )
        self._assert_platform_role(actor)
        task = await self._lock_task_scope(
            root_snapshot.project_id,
            root_snapshot.task_id,
            actor=actor,
            access=access,
            missing_detail=_FEEDBACK_UNAVAILABLE,
        )
        await self._lock_annotation(
            root_snapshot.annotation_id,
            project_id=root_snapshot.project_id,
            task_id=root_snapshot.task_id,
            creating=False,
        )
        entry, root = await self._lock_feedback_lineage(snapshots)
        return actor, access, entry, root, task

    async def _lock_task_scope(
        self,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        *,
        actor: IdentityActorFacts,
        access: ProjectAccess,
        missing_detail: str,
    ) -> Task | None:
        if task_id is None:
            return None
        with self.db.no_autoflush:
            target = (
                await execute_core_read(
                    self.db,
                    select(task_row.project_id, task_row.batch_id).where(
                        task_row.id == task_id
                    ),
                )
            ).one_or_none()
        if target is None or target.project_id != project_id:
            raise TaskWorkflowError(404, missing_detail)
        target_batch_id = target.batch_id
        if target_batch_id is not None:
            batches = await lock_core_nowait(
                self.db,
                lambda: lock_batch_rows_for_core_read(
                    self.db,
                    [target_batch_id],
                    project_id=project_id,
                    lock_mode="update",
                    nowait=True,
                    populate_existing=True,
                ),
                reason="feedback_batch_busy",
            )
            batch = batches[0] if batches else None
            if batch is None:
                raise TaskWorkflowError(404, missing_detail)
        task = await lock_core_nowait(
            self.db,
            lambda: lock_task_for_core_read(
                self.db,
                task_id,
                project_id=project_id,
                lock_mode="update",
                nowait=True,
                populate_existing=True,
            ),
            reason="feedback_task_busy",
        )
        if task is None:
            raise TaskWorkflowError(404, missing_detail)
        if task.batch_id != target_batch_id:
            raise TaskWorkflowError(409, {"reason": "task_target_changed"})
        await task_access.assert_task_visible(self.db, task, actor.id, access=access)
        return task

    async def _lock_annotation(
        self,
        annotation_id: uuid.UUID | None,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        creating: bool,
    ) -> Annotation | None:
        if annotation_id is None:
            return None
        annotations = await lock_core_nowait(
            self.db,
            lambda: lock_annotation_rows_for_core_read(
                self.db,
                [annotation_id],
                lock_mode="update",
                nowait=True,
                populate_existing=True,
            ),
            reason="feedback_annotation_busy",
        )
        annotation = annotations[0] if annotations else None
        if annotation is None:
            raise TaskWorkflowError(
                404, "Annotation not found" if creating else _FEEDBACK_UNAVAILABLE
            )
        if annotation.project_id != project_id or annotation.task_id != task_id:
            if creating:
                raise TaskWorkflowError(
                    422, "Feedback annotation does not belong to task"
                )
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        return annotation

    async def _read_annotation_scope(
        self,
        annotation_id: uuid.UUID | None,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
    ) -> Annotation | None:
        if annotation_id is None:
            return None
        annotation = await get_core_row(
            self.db, annotation_row, annotation_id, populate_existing=True
        )
        if (
            annotation is None
            or annotation.project_id != project_id
            or annotation.task_id != task_id
        ):
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        return annotation

    async def _read_task_fresh(self, task_id: uuid.UUID) -> Task | None:
        return (
            await execute_core_read(
                self.db,
                select(task_row)
                .where(task_row.id == task_id)
                .execution_options(populate_existing=True),
            )
        ).scalar_one_or_none()

    async def _read_feedback_lineage(
        self,
        entry_id: uuid.UUID,
        *,
        missing_detail: str,
    ) -> list[_FeedbackSnapshot]:
        snapshots: list[_FeedbackSnapshot] = []
        seen: set[uuid.UUID] = set()
        current_id = entry_id
        while True:
            if current_id in seen:
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
            seen.add(current_id)
            row = await self._read_feedback_snapshot(current_id)
            if row is None:
                raise TaskWorkflowError(
                    404,
                    missing_detail if not snapshots else _FEEDBACK_UNAVAILABLE,
                )
            if not snapshots and not row.is_active:
                raise TaskWorkflowError(404, _FEEDBACK_NOT_FOUND)
            snapshots.append(row)
            if row.thread_parent_id is None:
                break
            current_id = row.thread_parent_id
        root = snapshots[-1]
        if not root.is_active:
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        for child, parent in zip(snapshots, snapshots[1:]):
            if not self._same_scope(child, parent):
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        return snapshots

    async def _read_feedback_snapshot(
        self, entry_id: uuid.UUID
    ) -> _FeedbackSnapshot | None:
        row = await self.feedback.read_snapshot(entry_id)
        return _FeedbackSnapshot(*row) if row is not None else None

    async def _lock_feedback_lineage(
        self, snapshots: list[_FeedbackSnapshot]
    ) -> tuple[AnnotationFeedback, AnnotationFeedback]:
        ids = sorted({snapshot.id for snapshot in snapshots})
        rows = await self.feedback.lock_lineage(ids)
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(ids):
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        for snapshot in snapshots:
            row = by_id[snapshot.id]
            if (
                row.kind != snapshot.kind
                or row.anchor_type != snapshot.anchor_type
                or row.project_id != snapshot.project_id
                or row.task_id != snapshot.task_id
                or row.annotation_id != snapshot.annotation_id
                or row.anchor_position != snapshot.anchor_position
                or row.thread_parent_id != snapshot.thread_parent_id
            ):
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        for child_snapshot, parent_snapshot in zip(snapshots, snapshots[1:]):
            if not self._same_scope(
                by_id[child_snapshot.id], by_id[parent_snapshot.id]
            ):
                raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        entry = by_id[snapshots[0].id]
        root = by_id[snapshots[-1].id]
        if not entry.is_active:
            raise TaskWorkflowError(404, _FEEDBACK_NOT_FOUND)
        if not root.is_active:
            raise TaskWorkflowError(404, _FEEDBACK_UNAVAILABLE)
        return entry, root

    async def _lock_mask_qc_issue(self, issue_id: uuid.UUID) -> MaskQCIssue | None:
        try:
            with self.db.no_autoflush:
                return await load_mask_qc_issue(
                    self.db,
                    issue_id=issue_id,
                    lock=True,
                    nowait=True,
                    flush_before=False,
                )
        except HTTPException as exc:
            if exc.status_code == 409 and exc.detail == {"reason": "task_busy"}:
                raise TaskWorkflowError(
                    409, {"reason": "feedback_mask_qc_issue_busy"}
                ) from exc
            raise

    async def _lock_point_cloud_issue(
        self, issue_id: uuid.UUID
    ) -> PointCloudQualityIssue | None:
        try:
            data = await spatial_quality_data(
                self.db, "lock_point_issue", issue_id=issue_id
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise TaskWorkflowError(
                    409, {"reason": "feedback_point_cloud_issue_busy"}
                ) from exc
            raise
        return quality_row(PointCloudQualityIssue, data["row"]) if data["row"] else None

    async def _assert_reply_anchor_is_current(
        self,
        parent: AnnotationFeedback,
        *,
        actor: IdentityActorFacts,
        task: Task | None,
    ) -> None:
        mask_qc_issue_id = (parent.anchor_position or {}).get("mask_qc_issue_id")
        if mask_qc_issue_id:
            issue = await self._lock_mask_qc_issue(uuid.UUID(str(mask_qc_issue_id)))
            if issue is None or await effective_issue_status(self.db, issue) == "stale":
                raise TaskWorkflowError(409, {"reason": "mask_qc_issue_stale"})
        point_cloud_issue_id = (parent.anchor_position or {}).get(
            "point_cloud_quality_issue_id"
        )
        if point_cloud_issue_id:
            issue = await self._lock_point_cloud_issue(
                uuid.UUID(str(point_cloud_issue_id))
            )
            if issue is None or issue.task_id is None:
                raise TaskWorkflowError(
                    409, {"reason": "point_cloud_quality_issue_stale"}
                )
            issue_task = task if task is not None and task.id == issue.task_id else None
            if issue_task is None:
                issue_task = await self._read_task_fresh(issue.task_id)
            if issue_task is None:
                raise TaskWorkflowError(
                    409, {"reason": "point_cloud_quality_issue_stale"}
                )
            if task is None or issue_task.id != task.id:
                _project, access = await resolve_project_access_by_id(
                    self.db, actor_id=actor.id, project_id=issue_task.project_id
                )
                await task_access.assert_task_visible(
                    self.db, issue_task, actor.id, access=access
                )
            if await refresh_issue_staleness(self.db, issue):
                raise TaskWorkflowError(
                    409, {"reason": "point_cloud_quality_issue_stale"}
                )

    @staticmethod
    def _same_scope(
        left: _FeedbackSnapshot | AnnotationFeedback,
        right: _FeedbackSnapshot | AnnotationFeedback,
    ) -> bool:
        return (
            left.project_id == right.project_id
            and left.task_id == right.task_id
            and left.annotation_id == right.annotation_id
            and left.anchor_type == right.anchor_type
        )

    @staticmethod
    def _assert_canvas_drawing_scope(
        payload: AnnotationFeedbackCreate, task: Task | None
    ) -> None:
        if payload.canvas_drawing is None:
            return
        if task is None or task.file_type != "image":
            raise TaskWorkflowError(422, "canvas_drawing requires an image task")
