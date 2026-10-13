"""I18 · AnnotationFeedback service.

ADR-0027 第二阶段 (v0.10.20): 旧 bug_reports / annotation_comments / tasks.reject_reason
写路径加双写, 通过 mirror_* helper 在同事务内 INSERT 新表 + 失败一起回滚.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, any_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID, array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.schemas.annotation_feedback import FeedbackAnchorPosition

logger = logging.getLogger(__name__)


class FeedbackService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        author_id: uuid.UUID,
        kind: str,
        anchor_type: str,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        annotation_id: uuid.UUID | None,
        anchor_position: dict | None,
        severity: str | None,
        title: str | None,
        body: str,
        attachments: list[dict],
        thread_parent_id: uuid.UUID | None,
        canvas_drawing: dict | None = None,
        mentions: list[dict] | None = None,
    ) -> AnnotationFeedback:
        if anchor_position and anchor_position.get("video_context") is not None:
            # A reply copies a persisted anchor, including future versions or a
            # reference whose object/media has since changed. It never edits it.
            parent = (
                await self.db.get(AnnotationFeedback, thread_parent_id)
                if kind == "comment" and thread_parent_id is not None
                else None
            )
            inherits_anchor = (
                parent is not None
                and parent.is_active
                and parent.anchor_type == anchor_type
                and parent.project_id == project_id
                and parent.task_id == task_id
                and parent.annotation_id == annotation_id
                and parent.anchor_position == anchor_position
            )
            if not inherits_anchor:
                await self._validate_video_context(
                    anchor_type=anchor_type,
                    project_id=project_id,
                    task_id=task_id,
                    annotation_id=annotation_id,
                    anchor_position=anchor_position,
                )
        entry = AnnotationFeedback(
            kind=kind,
            anchor_type=anchor_type,
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            anchor_position=anchor_position,
            severity=severity,
            title=title,
            body=body,
            attachments=attachments,
            mentions=mentions or [],
            canvas_drawing=canvas_drawing,
            thread_parent_id=thread_parent_id,
            author_id=author_id,
            status="open",
            is_active=True,
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def _validate_video_context(
        self,
        *,
        anchor_type: str,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        annotation_id: uuid.UUID | None,
        anchor_position: dict,
    ) -> None:
        if anchor_type != "pixel" or task_id is None:
            raise HTTPException(
                status_code=422, detail="video_context requires a task pixel anchor"
            )
        try:
            anchor = FeedbackAnchorPosition.model_validate(anchor_position)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid video_context anchor"
            ) from exc
        context = anchor.video_context
        if (
            context is None
            or anchor.x is None
            or anchor.y is None
            or anchor.frame is None
        ):
            raise HTTPException(
                status_code=422, detail="video_context requires x, y and frame"
            )
        if context.annotation_version is not None and annotation_id is None:
            raise HTTPException(
                status_code=422, detail="annotation_version requires annotation_id"
            )

        task = await self.db.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.project_id != project_id:
            raise HTTPException(
                status_code=422, detail="Feedback task does not belong to project"
            )
        if task.file_type != "video":
            raise HTTPException(
                status_code=422, detail="video_context requires a video task"
            )
        if annotation_id is not None:
            annotation = await self.db.get(Annotation, annotation_id)
            if (
                annotation is None
                or not annotation.is_active
                or annotation.was_cancelled
            ):
                raise HTTPException(status_code=404, detail="Annotation not found")
            if annotation.task_id != task_id or annotation.project_id != project_id:
                raise HTTPException(
                    status_code=422,
                    detail="Feedback annotation does not belong to task",
                )
            # annotation_version is the captured version, not an optimistic-lock precondition.

        from app.services.video_frame_service import build_context_from_task

        media = await build_context_from_task(self.db, task)
        frame_count = media.metadata.frame_count
        if frame_count is None or frame_count < 1:
            raise HTTPException(status_code=503, detail="Video metadata not ready")
        last_frame = frame_count - 1
        if anchor.frame > last_frame:
            raise HTTPException(
                status_code=422, detail="Feedback frame is outside video"
            )
        frame_range = context.frame_range
        if frame_range is not None and not (
            0
            <= frame_range.from_frame
            <= anchor.frame
            <= frame_range.to_frame
            <= last_frame
        ):
            raise HTTPException(
                status_code=422,
                detail="Feedback frame range must contain frame within video",
            )
        window = context.timeline_window
        if window is not None and not (0 <= window.from_ <= window.to <= last_frame):
            raise HTTPException(
                status_code=422,
                detail="Feedback timeline window is outside video or unordered",
            )

    async def patch(
        self,
        feedback_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        status: str | None = None,
        severity: str | None = None,
        title: str | None = None,
        body: str | None = None,
        mentions: list[dict] | None = None,
    ) -> AnnotationFeedback:
        entry = await self.db.get(AnnotationFeedback, feedback_id)
        if entry is None or not entry.is_active:
            raise HTTPException(status_code=404, detail="feedback not found")
        if status is not None and status != entry.status:
            entry.status = status
            if status in ("resolved", "wont_fix"):
                entry.resolved_at = datetime.now(timezone.utc)
                entry.resolved_by_id = actor_id
            else:
                entry.resolved_at = None
                entry.resolved_by_id = None
        if severity is not None:
            entry.severity = severity
        if title is not None:
            entry.title = title
        if body is not None:
            body_changed = body != entry.body
            entry.body = body
            if mentions is not None:
                entry.mentions = mentions
            elif body_changed:
                # Body edits without an editor mention map cannot preserve offsets
                # safely; clear them rather than leaving stale chips.
                entry.mentions = []
        elif mentions is not None:
            entry.mentions = mentions
        await self.db.flush()
        return entry

    # ------------------------------------------------------------------
    # ADR-0027 第二段 (v0.10.20) · 旧三表双写 mirror helpers
    #
    # 设计: 各 helper 接受 legacy 模型实例, 提取字段构造 AnnotationFeedback INSERT
    # 在同一 session/事务中执行. 调用方在 legacy 写后立刻调, db.commit() 时一起落库;
    # 任一失败 → 整体回滚, 不留半边写.
    # ------------------------------------------------------------------

    async def mirror_bug_report(self, bug_report) -> AnnotationFeedback:
        """legacy bug_reports.INSERT 后镜像写入 annotation_feedbacks (kind=bug)."""
        anchor_type = "task" if bug_report.task_id is not None else "project"
        # project_id NOT NULL in annotation_feedbacks; bug_reports.project_id may be null
        # → 该情况下不 mirror (无项目归属的 bug 仍只在 bug_reports 存在; v0.10.21 切单源时
        #   单独处理 — 那批通常是登录页 / 未进项目的 bug 反馈)
        if bug_report.project_id is None:
            logger.debug(
                "[ADR-0027] skip mirror bug_report=%s (project_id is null)",
                bug_report.id,
            )
            return None  # type: ignore[return-value]
        feedback = AnnotationFeedback(
            kind="bug",
            anchor_type=anchor_type,
            project_id=bug_report.project_id,
            task_id=bug_report.task_id,
            annotation_id=None,
            anchor_position=None,
            status="open",
            severity=bug_report.severity,
            title=bug_report.title,
            body=bug_report.description,
            attachments=[],
            mentions=[],
            thread_parent_id=None,
            author_id=bug_report.reporter_id,
            is_active=True,
        )
        self.db.add(feedback)
        await self.db.flush()
        logger.info(
            "[ADR-0027 double-write] bug_reports %s → feedback %s",
            bug_report.id,
            feedback.id,
        )
        return feedback

    async def mirror_annotation_comment(
        self, comment, *, task_id: uuid.UUID
    ) -> AnnotationFeedback:
        """legacy annotation_comments.INSERT 后镜像写入 (kind=comment, anchor=annotation).

        task_id 由调用方从 Annotation 实例取出 (annotation_feedbacks.anchor_type='annotation'
        CHECK 要求 task_id NOT NULL).
        """
        feedback = AnnotationFeedback(
            kind="comment",
            anchor_type="annotation",
            project_id=comment.project_id,
            task_id=task_id,
            annotation_id=comment.annotation_id,
            anchor_position=None,
            status="open",
            severity=None,
            title=None,
            body=comment.body,
            attachments=comment.attachments or [],
            mentions=comment.mentions or [],
            thread_parent_id=None,
            author_id=comment.author_id,
            is_active=True,
        )
        self.db.add(feedback)
        await self.db.flush()
        logger.info(
            "[ADR-0027 double-write] annotation_comments %s → feedback %s",
            comment.id,
            feedback.id,
        )
        return feedback

    async def mirror_task_reject(
        self,
        task,
        *,
        reviewer_id: uuid.UUID,
    ) -> AnnotationFeedback:
        """legacy tasks.reject_reason 设置后镜像写入 (kind=reject, anchor=task).

        reject_reason_type → severity (info/warn/blocker 不强制, 沿用枚举原值).
        """
        feedback = AnnotationFeedback(
            kind="reject",
            anchor_type="task",
            project_id=task.project_id,
            task_id=task.id,
            annotation_id=None,
            anchor_position=None,
            status="open",
            severity=task.reject_reason_type,
            title=None,
            body=task.reject_reason or "",
            attachments=[],
            mentions=[],
            thread_parent_id=None,
            author_id=reviewer_id,
            is_active=True,
        )
        self.db.add(feedback)
        await self.db.flush()
        logger.info(
            "[ADR-0027 double-write] tasks.reject %s → feedback %s",
            task.id,
            feedback.id,
        )
        return feedback

    async def soft_delete(self, feedback_id: uuid.UUID) -> AnnotationFeedback:
        entry = await self.db.get(AnnotationFeedback, feedback_id)
        if entry is None or not entry.is_active:
            raise HTTPException(status_code=404, detail="feedback not found")
        entry.is_active = False
        await self.db.flush()
        return entry

    async def resolve_root(
        self,
        feedback_id: uuid.UUID,
        *,
        require_active_root: bool = True,
    ) -> AnnotationFeedback:
        """Resolve a feedback row to its root while validating the ancestry.

        The feedback table deliberately keeps a nullable self-reference rather
        than a materialized root column.  All mutation paths therefore use this
        bounded, cycle-aware walk before changing or appending a row.  Deleted
        intermediate replies are valid ancestry; only a deleted root makes the
        thread unavailable.
        """

        current = await self.db.get(AnnotationFeedback, feedback_id)
        if current is None:
            raise HTTPException(status_code=404, detail="feedback not found")

        seen: set[uuid.UUID] = set()
        while current.thread_parent_id is not None:
            if current.id in seen:
                raise HTTPException(
                    status_code=404, detail="feedback thread is unavailable"
                )
            seen.add(current.id)
            parent = await self.db.get(AnnotationFeedback, current.thread_parent_id)
            if parent is None:
                raise HTTPException(
                    status_code=404, detail="feedback thread is unavailable"
                )
            if not _same_scope(current, parent):
                raise HTTPException(
                    status_code=404, detail="feedback thread is unavailable"
                )
            current = parent

        if current.id in seen:
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )
        if require_active_root and not current.is_active:
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )
        return current

    async def validate_parent(
        self,
        parent_id: uuid.UUID,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        annotation_id: uuid.UUID | None,
        anchor_type: str,
        anchor_position: dict | None,
    ) -> tuple[AnnotationFeedback, AnnotationFeedback]:
        """Validate a direct-create parent exactly like the reply endpoint.

        Keeping this in the service prevents a caller from bypassing
        ``POST /feedbacks/{id}/replies`` by posting an arbitrary
        ``thread_parent_id`` to the generic create endpoint.
        """

        parent = await self.db.get(AnnotationFeedback, parent_id)
        if parent is None or not parent.is_active:
            raise HTTPException(status_code=404, detail="feedback not found")
        root = await self.resolve_root(parent_id)
        if (
            parent.project_id != project_id
            or parent.task_id != task_id
            or parent.annotation_id != annotation_id
            or parent.anchor_type != anchor_type
            or parent.anchor_position != anchor_position
        ):
            raise HTTPException(
                status_code=422,
                detail="reply parent must remain in the same feedback scope",
            )
        return parent, root

    async def list_paged(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        annotation_id: uuid.UUID | None = None,
        kind: str | None = None,
        anchor_type: str | None = None,
        status: str | None = None,
        allowed_task_ids: set[uuid.UUID] | None = None,
        cursor: str | None = None,
        limit: int = 50,
        root_only: bool = False,
    ) -> tuple[list[AnnotationFeedback], str | None]:
        q = self._scoped_query(
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            kind=kind,
            anchor_type=anchor_type,
            status=status,
            allowed_task_ids=allowed_task_ids,
            root_only=root_only,
        )
        if cursor:
            last_ts, last_id = _decode_cursor(cursor)
            q = q.where(
                or_(
                    AnnotationFeedback.created_at < last_ts,
                    and_(
                        AnnotationFeedback.created_at == last_ts,
                        AnnotationFeedback.id < last_id,
                    ),
                )
            )
        q = q.order_by(
            AnnotationFeedback.created_at.desc(), AnnotationFeedback.id.desc()
        ).limit(limit + 1)
        rows = list((await self.db.execute(q)).scalars().all())
        next_cursor: str | None = None
        if len(rows) > limit:
            anchor = rows[limit - 1]
            next_cursor = _encode_cursor(anchor.created_at, anchor.id)
            rows = rows[:limit]
        return rows, next_cursor

    async def count_paged(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        annotation_id: uuid.UUID | None = None,
        kind: str | None = None,
        anchor_type: str | None = None,
        status: str | None = None,
        allowed_task_ids: set[uuid.UUID] | None = None,
        root_only: bool = False,
    ) -> int:
        """Return the exact count using the same root/scope predicates as list."""

        scoped = self._scoped_query(
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            kind=kind,
            anchor_type=anchor_type,
            status=status,
            allowed_task_ids=allowed_task_ids,
            root_only=root_only,
        ).subquery()
        return int(
            (
                await self.db.execute(select(func.count()).select_from(scoped))
            ).scalar_one()
        )

    async def status_counts(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        annotation_id: uuid.UUID | None = None,
        kind: str | None = None,
        anchor_type: str | None = None,
        allowed_task_ids: set[uuid.UUID] | None = None,
        root_only: bool = False,
    ) -> dict[str, int]:
        """Count each status while intentionally ignoring the status filter."""

        scoped = self._scoped_query(
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            kind=kind,
            anchor_type=anchor_type,
            status=None,
            allowed_task_ids=allowed_task_ids,
            root_only=root_only,
        ).subquery()
        rows = (
            await self.db.execute(
                select(scoped.c.status, func.count())
                .group_by(scoped.c.status)
                .order_by(scoped.c.status)
            )
        ).all()
        counts = {status: int(count) for status, count in rows}
        return {status: counts.get(status, 0) for status in _FEEDBACK_STATUSES}

    async def thread_paged(
        self,
        root_feedback_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[AnnotationFeedback, list[AnnotationFeedback], str | None, int]:
        """Read active descendants of a root through deleted intermediates.

        The recursive CTE carries a UUID path to make malformed cyclic data
        finite and to prevent a cycle from duplicating a reply.  Deleted rows
        remain in the CTE as traversal nodes but are excluded from the returned
        items and total.
        """

        root = await self.db.get(AnnotationFeedback, root_feedback_id)
        if root is None or not root.is_active or root.thread_parent_id is not None:
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )

        descendants = self._descendants_cte(root)
        active_ids = (
            select(descendants.c.id).where(descendants.c.is_active.is_(True)).subquery()
        )
        count = int(
            (
                await self.db.execute(select(func.count()).select_from(active_ids))
            ).scalar_one()
        )

        q = (
            select(AnnotationFeedback)
            .join(active_ids, AnnotationFeedback.id == active_ids.c.id)
            .order_by(
                AnnotationFeedback.created_at.desc(),
                AnnotationFeedback.id.desc(),
            )
        )
        if cursor:
            cursor_root, last_ts, last_id = _decode_thread_cursor(cursor)
            if cursor_root != root.id:
                raise HTTPException(status_code=400, detail="invalid feedback cursor")
            q = q.where(
                or_(
                    AnnotationFeedback.created_at < last_ts,
                    and_(
                        AnnotationFeedback.created_at == last_ts,
                        AnnotationFeedback.id < last_id,
                    ),
                )
            )
        q = q.limit(limit + 1)
        rows = list((await self.db.execute(q)).scalars().all())
        next_cursor: str | None = None
        if len(rows) > limit:
            anchor = rows[limit - 1]
            next_cursor = _encode_thread_cursor(root.id, anchor.created_at, anchor.id)
            rows = rows[:limit]
        return root, rows, next_cursor, count

    def _scoped_query(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        annotation_id: uuid.UUID | None,
        kind: str | None,
        anchor_type: str | None,
        status: str | None,
        allowed_task_ids: set[uuid.UUID] | None,
        root_only: bool,
    ):
        """Build the canonical root-filtered feedback relation.

        A row is visible only when its terminal root is active and has the same
        project/task scope.  This deliberately excludes orphan descendants from
        both normal and root-only queries, while retaining descendants below an
        inactive *intermediate* reply.
        """

        af = AnnotationFeedback
        if root_only:
            # A root has no ancestry to traverse. Keep current-task badges and
            # pin pagination independent of unrelated replies in the project.
            q = select(af).where(
                af.project_id == project_id,
                af.is_active.is_(True),
                af.thread_parent_id.is_(None),
                _valid_root_clause(af),
            )
        else:
            root_map = self._root_map_cte(project_id)
            root = aliased(AnnotationFeedback)
            q = (
                select(af)
                .join(root_map, root_map.c.origin_id == af.id)
                .join(root, root.id == root_map.c.root_id)
                .where(
                    af.project_id == project_id,
                    af.is_active.is_(True),
                    root.is_active.is_(True),
                    root.project_id == project_id,
                    _same_scope_clause(af, root),
                    _same_anchor_scope_clause(af, root),
                    _valid_root_clause(root),
                )
            )
        if allowed_task_ids is not None:
            q = q.where(or_(af.task_id.is_(None), af.task_id.in_(allowed_task_ids)))
        if task_id is not None:
            q = q.where(af.task_id == task_id)
        if annotation_id is not None:
            q = q.where(af.annotation_id == annotation_id)
        if kind is not None:
            q = q.where(af.kind == kind)
        if anchor_type is not None:
            q = q.where(af.anchor_type == anchor_type)
        if status is not None:
            q = q.where(af.status == status)
        return q

    @staticmethod
    def _root_map_cte(project_id: uuid.UUID):
        af = AnnotationFeedback
        lineage = (
            select(
                af.id.label("origin_id"),
                af.id.label("ancestor_id"),
                af.thread_parent_id.label("next_parent_id"),
                af.project_id.label("origin_project_id"),
                af.task_id.label("origin_task_id"),
                af.anchor_type.label("origin_anchor_type"),
                af.annotation_id.label("origin_annotation_id"),
                cast(array([af.id]), ARRAY(PGUUID(as_uuid=True))).label("path"),
            )
            .where(af.project_id == project_id)
            .cte("feedback_lineage", recursive=True)
        )
        parent = aliased(AnnotationFeedback)
        lineage = lineage.union_all(
            select(
                lineage.c.origin_id,
                parent.id.label("ancestor_id"),
                parent.thread_parent_id.label("next_parent_id"),
                lineage.c.origin_project_id,
                lineage.c.origin_task_id,
                lineage.c.origin_anchor_type,
                lineage.c.origin_annotation_id,
                lineage.c.path.concat(array([parent.id])).label("path"),
            )
            .join(parent, parent.id == lineage.c.next_parent_id)
            .where(
                parent.project_id == lineage.c.origin_project_id,
                _same_optional_value(parent.task_id, lineage.c.origin_task_id),
                parent.anchor_type == lineage.c.origin_anchor_type,
                _same_optional_value(
                    parent.annotation_id, lineage.c.origin_annotation_id
                ),
                ~(parent.id == any_(lineage.c.path)),
            )
        )
        return (
            select(
                lineage.c.origin_id,
                lineage.c.ancestor_id.label("root_id"),
            )
            .where(lineage.c.next_parent_id.is_(None))
            .cte("feedback_roots")
        )

    @staticmethod
    def _descendants_cte(root: AnnotationFeedback):
        af = AnnotationFeedback
        descendants = (
            select(
                af.id.label("id"),
                af.thread_parent_id.label("parent_id"),
                af.project_id.label("project_id"),
                af.task_id.label("task_id"),
                af.is_active.label("is_active"),
                cast(array([af.id]), ARRAY(PGUUID(as_uuid=True))).label("path"),
            )
            .where(
                af.thread_parent_id == root.id,
                af.project_id == root.project_id,
                _same_task_value(af.task_id, root.task_id),
                af.anchor_type == root.anchor_type,
                _same_optional_value(af.annotation_id, root.annotation_id),
            )
            .cte("feedback_descendants", recursive=True)
        )
        child = aliased(AnnotationFeedback)
        descendants = descendants.union_all(
            select(
                child.id,
                child.thread_parent_id,
                child.project_id,
                child.task_id,
                child.is_active,
                descendants.c.path.concat(array([child.id])).label("path"),
            )
            .select_from(descendants)
            .join(child, child.thread_parent_id == descendants.c.id)
            .where(
                child.project_id == root.project_id,
                _same_task_value(child.task_id, root.task_id),
                child.anchor_type == root.anchor_type,
                _same_optional_value(child.annotation_id, root.annotation_id),
                ~(child.id == any_(descendants.c.path)),
            )
        )
        return descendants


def _encode_cursor(created_at: datetime, fid: uuid.UUID) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    ts = created_at.astimezone(timezone.utc).isoformat()
    return base64.urlsafe_b64encode(f"{ts}|{fid.hex}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
        raise HTTPException(status_code=400, detail="invalid feedback cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 2:
            raise ValueError("unexpected cursor fields")
        ts = datetime.fromisoformat(parts[0])
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("cursor timestamp must include timezone")
        fid = uuid.UUID(parts[1])
        return ts, fid
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid feedback cursor") from exc


def _encode_thread_cursor(
    root_id: uuid.UUID, created_at: datetime, fid: uuid.UUID
) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    ts = created_at.astimezone(timezone.utc).isoformat()
    raw = f"thread-v1|{root_id.hex}|{ts}|{fid.hex}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_thread_cursor(
    cursor: str,
) -> tuple[uuid.UUID, datetime, uuid.UUID]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
        raise HTTPException(status_code=400, detail="invalid feedback cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
        version, root_id, ts_str, feedback_id = raw.split("|")
        if version != "thread-v1":
            raise ValueError("unexpected cursor version")
        timestamp = datetime.fromisoformat(ts_str)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("cursor timestamp must include timezone")
        return (
            uuid.UUID(root_id),
            timestamp,
            uuid.UUID(feedback_id),
        )
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid feedback cursor") from exc


_FEEDBACK_STATUSES = ("open", "resolved", "wont_fix")


def _same_task_value(left, right):
    """Build a SQL null-safe equality expression for task scope."""

    return _same_optional_value(left, right)


def _same_scope(left: AnnotationFeedback, right: AnnotationFeedback) -> bool:
    if left.project_id != right.project_id:
        return False
    return (
        left.task_id == right.task_id
        and left.anchor_type == right.anchor_type
        and left.annotation_id == right.annotation_id
    )


def _same_scope_clause(left: AnnotationFeedback, right: AnnotationFeedback):
    return and_(
        left.project_id == right.project_id,
        _same_task_value(left.task_id, right.task_id),
    )


def _same_optional_value(left, right):
    if right is None:
        return left.is_(None)
    if left is None:
        return right.is_(None)
    if hasattr(left, "is_not_distinct_from"):
        return left.is_not_distinct_from(right)
    return right.is_not_distinct_from(left)


def _same_anchor_scope_clause(left: AnnotationFeedback, right: AnnotationFeedback):
    return and_(
        left.anchor_type == right.anchor_type,
        _same_optional_value(left.annotation_id, right.annotation_id),
    )


def _valid_root_clause(root: AnnotationFeedback):
    """Reject malformed roots that point at another task/project annotation."""

    from app.db.models.annotation import Annotation
    from app.db.models.task import Task

    # Aliased tables keep the anti-orphan checks independent of any outer
    # Task/Annotation in correlated contexts (Data Manager counts).
    root_task = aliased(Task)
    root_annotation = aliased(Annotation)
    task_ok = (
        ~select(root_task.id)
        .where(
            root_task.id == root.task_id,
            root_task.project_id != root.project_id,
        )
        .exists()
    )
    annotation_ok = (
        ~select(root_annotation.id)
        .where(
            root_annotation.id == root.annotation_id,
            or_(
                root_annotation.project_id != root.project_id,
                ~_same_task_value(root_annotation.task_id, root.task_id),
            ),
        )
        .exists()
    )
    return and_(task_ok, annotation_ok)


def open_root_issue_clause(af: AnnotationFeedback) -> ColumnElement[bool]:
    """Predicate for active, valid, open root issues.

    Matches the Workbench issue relation (``kind=issue``, root only, open) so
    task-level issue counts agree with ``DiscussionIssuesTab``. Replies,
    ordinary comments, resolved/shelved/deleted issues, bugs and rejection
    records are excluded.
    """

    return and_(
        af.kind == "issue",
        af.thread_parent_id.is_(None),
        af.is_active.is_(True),
        af.status == "open",
        _valid_root_clause(af),
    )


def unresolved_issue_count_sq() -> ColumnElement[int]:
    """Correlated count of active open root issues per ``Task`` row."""

    af = AnnotationFeedback
    return (
        select(func.count(af.id))
        .where(
            af.project_id == Task.project_id,
            af.task_id == Task.id,
            open_root_issue_clause(af),
        )
        .scalar_subquery()
    )


def annotation_unresolved_issue_count_sq(annotation) -> ColumnElement[int]:
    """Correlated count of active open root issues anchored to one annotation."""

    af = AnnotationFeedback
    return (
        select(func.count(af.id))
        .where(
            af.annotation_id == annotation.id,
            open_root_issue_clause(af),
        )
        .correlate(annotation)
        .scalar_subquery()
    )
