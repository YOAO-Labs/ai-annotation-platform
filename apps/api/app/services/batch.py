from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Integer, case, select, func, update, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.user import User
from app.schemas.batch import BatchCreate, BatchUpdate, BatchSplitRequest
from app.services.display_id import next_display_id
from app.services.progress import (
    publish_batch_assignment_change,
    publish_batch_status_change,
)
from app.services.project_access import membership_role_compatible
from app.services.scene import resolve_task_scene_frames

# v0.16.x 拆分：角色权限守卫已抽到 batch_permissions.py，此处冗余别名 re-export，
# 保持 `from app.services.batch import assert_can_transition/REVERSE_TRANSITIONS/...` 旧入口不变。
from app.services.batch_permissions import (
    REVERSE_TRANSITIONS as REVERSE_TRANSITIONS,
    _is_annotator_assigned as _is_annotator_assigned,
    _is_owner as _is_owner,
    _is_reviewer as _is_reviewer,
    assert_can_transition as assert_can_transition,
)

logger = logging.getLogger(__name__)

# v0.12.0 B5 · split 回写 batch_id 的分块大小，避免十万级未归类「一键全量建包」
# 打成单条巨 IN/UPDATE。模块级常量便于测试覆盖分块边界。
ASSIGN_CHUNK_SIZE = 5000

VALID_TRANSITIONS: dict[str, set[str]] = {
    BatchStatus.DRAFT: {BatchStatus.ACTIVE},
    # v0.9.5：active → pre_annotated 由 batch_predict task 末尾自动触发
    BatchStatus.ACTIVE: {
        BatchStatus.ANNOTATING,
        BatchStatus.PRE_ANNOTATED,
        BatchStatus.ARCHIVED,
    },
    # v0.9.5：pre_annotated → annotating 与 active 同语义（scheduler 自动驱动）；
    # → active 是 owner 兜底重置（逆向，丢弃预标 predictions）；→ archived 任意 owner
    BatchStatus.PRE_ANNOTATED: {
        BatchStatus.ANNOTATING,
        BatchStatus.ACTIVE,
        BatchStatus.ARCHIVED,
    },
    BatchStatus.ANNOTATING: {BatchStatus.REVIEWING, BatchStatus.ARCHIVED},
    BatchStatus.REVIEWING: {BatchStatus.APPROVED, BatchStatus.REJECTED},
    # v0.7.3：approved → reviewing 重开审核（owner 兜底，需 reason）
    BatchStatus.APPROVED: {BatchStatus.ARCHIVED, BatchStatus.REVIEWING},
    # v0.7.3：rejected → reviewing 跳过重标直接复审（owner 兜底，需 reason）
    BatchStatus.REJECTED: {
        BatchStatus.ACTIVE,
        BatchStatus.ARCHIVED,
        BatchStatus.REVIEWING,
    },
    # v0.7.3：archived → active 撤销归档（owner 兜底，需 reason）；后续由 scheduler 自动推进到正确阶段
    BatchStatus.ARCHIVED: {BatchStatus.ACTIVE},
}


class BatchService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _lock_and_validate_assignment_targets(
        self,
        project_id: uuid.UUID,
        targets: list[tuple[str, uuid.UUID]],
    ) -> None:
        """Lock and validate every user that will receive batch work.

        User rows are locked before any batch or task row in all assignment
        paths. Offboarding uses nonblocking locks and either retries a busy
        account or completes before this method rechecks its state. Membership is
        checked separately because the member role is part of the assignment
        contract, not merely a UI filter.
        """
        if not targets:
            return

        target_ids = sorted({user_id for _, user_id in targets})
        users = (
            (
                await self.db.execute(
                    select(User)
                    .where(User.id.in_(target_ids))
                    .order_by(User.id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        users_by_id = {user.id: user for user in users}

        members = (
            (
                await self.db.execute(
                    select(ProjectMember)
                    .where(
                        ProjectMember.project_id == project_id,
                        ProjectMember.user_id.in_(target_ids),
                    )
                    .order_by(ProjectMember.user_id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        members_by_user_id = {member.user_id: member for member in members}

        # Responsibility is project-scoped: the membership role is the
        # authority and the account only needs a compatible platform role
        # (employee/legacy staff).  The old global role equality is gone.
        for assignment_role, user_id in targets:
            user = users_by_id.get(user_id)
            if user is None or not user.is_active:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "assignment_user_unavailable",
                        "message": "接收账号已停用或不存在，请刷新成员列表后重新分派",
                        "assignment_role": assignment_role,
                        "user_id": str(user_id),
                    },
                )
            member = members_by_user_id.get(user_id)
            if member is None or member.role != assignment_role:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "assignment_project_member_required",
                        "message": "接收账号不具备目标项目的对应成员职责",
                        "assignment_role": assignment_role,
                        "user_id": str(user_id),
                    },
                )
            if not membership_role_compatible(user.role, assignment_role):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "assignment_role_mismatch",
                        "message": "接收账号的平台角色与分派职责不匹配",
                        "assignment_role": assignment_role,
                        "user_id": str(user_id),
                        "user_role": user.role,
                    },
                )

    # ── Queries ────────────────────────────────────────────────────────────

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        status: str | None = None,
    ) -> list[TaskBatch]:
        q = select(TaskBatch).where(TaskBatch.project_id == project_id)
        if status:
            # issue #124 · 允许逗号分隔多状态（如 "active,draft"），让调用方只取
            # 需要的批次而不是全量历史；单状态（"active"）行为不变。
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if statuses:
                q = q.where(TaskBatch.status.in_(statuses))
        q = q.order_by(TaskBatch.priority.desc(), TaskBatch.created_at)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def get(self, batch_id: uuid.UUID) -> TaskBatch | None:
        return await self.db.get(TaskBatch, batch_id)

    async def get_default_batch(self, project_id: uuid.UUID) -> TaskBatch | None:
        result = await self.db.execute(
            select(TaskBatch).where(
                TaskBatch.project_id == project_id,
                TaskBatch.display_id == "B-DEFAULT",
            )
        )
        return result.scalar_one_or_none()

    async def _splittable_task_ids(
        self,
        project_id: uuid.UUID,
        default: TaskBatch | None,
    ) -> list[uuid.UUID]:
        # v0.6.8 B-14：可被 split 的「未归类任务」= batch_id IS NULL ∪ 老项目残留 B-DEFAULT 中的任务。
        # v0.6.7 后新项目不再有 B-DEFAULT；删完批次后任务回退为 batch_id=NULL，仍能被 split。
        conds = [Task.batch_id.is_(None)]
        if default is not None:
            conds.append(Task.batch_id == default.id)
        # v0.11.22：按创建顺序返回，使「顺序切分（不打乱）」结果稳定可预期。
        result = await self.db.execute(
            select(Task.id)
            .where(Task.project_id == project_id)
            .where(or_(*conds))
            .order_by(Task.created_at, Task.id)
        )
        return [row[0] for row in result.fetchall()]

    # ── Mutations ──────────────────────────────────────────────────────────

    async def create(
        self,
        project_id: uuid.UUID,
        data: BatchCreate,
        created_by: uuid.UUID,
    ) -> TaskBatch:
        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if data.annotator_id is not None:
            assignment_targets.append(("annotator", data.annotator_id))
        if data.reviewer_id is not None:
            assignment_targets.append(("reviewer", data.reviewer_id))
        await self._lock_and_validate_assignment_targets(project_id, assignment_targets)
        batch = TaskBatch(
            project_id=project_id,
            dataset_id=data.dataset_id,
            display_id=await next_display_id(self.db, "batches"),
            name=data.name,
            description=data.description,
            status=BatchStatus.DRAFT,
            priority=data.priority,
            deadline=data.deadline,
            annotator_id=data.annotator_id,
            reviewer_id=data.reviewer_id,
            created_by=created_by,
        )
        self._sync_assigned_user_ids(batch)
        self.db.add(batch)
        await self.db.flush()
        return batch

    async def update(
        self,
        batch_id: uuid.UUID,
        data: BatchUpdate,
        *,
        project_id: uuid.UUID | None = None,
    ) -> TaskBatch:
        fields = data.model_dump(exclude_unset=True)

        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if "annotator_id" in fields and data.annotator_id is not None:
            assignment_targets.append(("annotator", data.annotator_id))
        if "reviewer_id" in fields and data.reviewer_id is not None:
            assignment_targets.append(("reviewer", data.reviewer_id))
        if assignment_targets:
            if project_id is None:
                probe = await self.db.get(TaskBatch, batch_id)
                if probe is None:
                    raise HTTPException(status_code=404, detail="Batch not found")
                project_id = probe.project_id
            await self._lock_and_validate_assignment_targets(
                project_id, assignment_targets
            )
            stmt = (
                select(TaskBatch)
                .where(TaskBatch.id == batch_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if project_id is not None:
                stmt = stmt.where(TaskBatch.project_id == project_id)
            batch = (await self.db.execute(stmt)).scalar_one_or_none()
        else:
            batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        for k, v in fields.items():
            setattr(batch, k, v)

        # v0.7.2：annotator_id / reviewer_id 变更时，自动派生 assigned_user_ids
        # （前端旧路径仍读这个 list；同时回填 task 级 assignee_id / reviewer_id）
        annotator_changed = "annotator_id" in fields
        reviewer_changed = "reviewer_id" in fields
        if annotator_changed or reviewer_changed:
            self._sync_assigned_user_ids(batch)

        await self.db.flush()

        if annotator_changed:
            await self._cascade_task_assignee(batch.id, batch.annotator_id)
        if reviewer_changed:
            await self._cascade_task_reviewer(batch.id, batch.reviewer_id)
        # issue #124 · 改派不改变 batch.status，不会触发 status_changed；单独广播让
        # 其它管理员的「可预标」列表实时收敛（draft 的分派会改变准入资格）。
        if annotator_changed or reviewer_changed:
            await publish_batch_assignment_change(
                str(batch.project_id), [str(batch.id)]
            )
        return batch

    @staticmethod
    def _sync_assigned_user_ids(batch: TaskBatch) -> None:
        """v0.7.2：派生 assigned_user_ids = [annotator_id, reviewer_id] filter None。
        旧的 multi-select 数据由 alembic 0030 已迁移到单值列；此处保持双向写。
        """
        ids: list[str] = []
        if batch.annotator_id:
            ids.append(str(batch.annotator_id))
        if batch.reviewer_id:
            ids.append(str(batch.reviewer_id))
        batch.assigned_user_ids = ids

    async def _cascade_task_assignee(
        self,
        batch_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> None:
        """Update inherited assignments without overwriting selected tasks."""
        values: dict[str, Any] = {
            "assignee_id": user_id,
            "assignee_is_override": False,
        }
        values["assigned_at"] = func.now() if user_id is not None else None
        await self.db.execute(
            update(Task)
            .where(
                Task.batch_id == batch_id,
                or_(Task.assignee_is_override.is_(False), Task.assignee_id.is_(None)),
            )
            .values(**values)
        )

    async def _cascade_task_reviewer(
        self,
        batch_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> None:
        """Update inherited reviewers without overwriting selected tasks."""
        await self.db.execute(
            update(Task)
            .where(
                Task.batch_id == batch_id,
                or_(Task.reviewer_is_override.is_(False), Task.reviewer_id.is_(None)),
            )
            .values(reviewer_id=user_id, reviewer_is_override=False)
        )

    async def transition(
        self,
        batch_id: uuid.UUID,
        target_status: str,
        actor_id: uuid.UUID | None = None,
    ) -> TaskBatch:
        is_decision = target_status in (BatchStatus.APPROVED, BatchStatus.REJECTED)
        if is_decision:
            locked = await self._lock_decision_scope(actor_id, [batch_id])
            batch = locked.get(batch_id)
        else:
            batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        allowed = VALID_TRANSITIONS.get(batch.status, set())
        if target_status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transition from '{batch.status}' to '{target_status}'",
            )

        # v0.7.0：draft → active 必须有任务（拒绝空批次激活）
        if batch.status == BatchStatus.DRAFT and target_status == BatchStatus.ACTIVE:
            count = (
                await self.db.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.batch_id == batch_id)
                )
            ).scalar() or 0
            if count == 0:
                raise HTTPException(
                    status_code=400, detail="cannot activate empty batch"
                )

        # v0.7.3：approved → reviewing 重开审核 — 清空原审核元数据（reviewed_at / reviewed_by / review_feedback）
        # rejected → reviewing 不清反馈：复审时 reviewer 需要看到上次原因
        if is_decision:
            await self._assert_locked_batch_evidence(batch, actor_id)
        from_status = batch.status
        if (from_status, target_status) == (
            BatchStatus.APPROVED,
            BatchStatus.REVIEWING,
        ):
            batch.review_feedback = None
            batch.reviewed_at = None
            batch.reviewed_by = None

        batch.status = target_status
        await self.db.flush()

        # v0.9.13 · 广播 batch 状态变更, 前端 useBatchEventsSocket invalidate ["batches", projectId]
        if from_status != target_status:
            await publish_batch_status_change(
                str(batch.project_id),
                str(batch_id),
                from_status,
                target_status,
            )

        if target_status == BatchStatus.APPROVED:
            await self.on_batch_approved(batch_id)

        return batch

    async def _count_protected_tasks(self, batch_id: uuid.UUID) -> tuple[int, int, int]:
        """v0.11.25 · 统计「有进行中成果」的 task: 非 pending 数 / 已预标数 / 去重后实际受影响数。
        non_pending 与 predicted 可能重叠（同一 task 既非 pending 又已预标），affected 是二者的
        并集去重计数，供前端展示「将影响 N 个任务」避免 X+Y 误判。供 delete / bulk_delete 保护判定。"""
        row = (
            await self.db.execute(
                select(
                    func.count().filter(Task.status != "pending").label("non_pending"),
                    func.count().filter(Task.total_predictions > 0).label("predicted"),
                    func.count()
                    .filter(or_(Task.status != "pending", Task.total_predictions > 0))
                    .label("affected"),
                ).where(Task.batch_id == batch_id)
            )
        ).one()
        return int(row.non_pending), int(row.predicted), int(row.affected)

    async def delete(
        self,
        batch_id: uuid.UUID,
        *,
        force: bool = False,
        actor_id: uuid.UUID | None = None,
    ) -> bool:
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        if batch.display_id == "B-DEFAULT":
            raise HTTPException(
                status_code=400, detail="Cannot delete the default batch"
            )

        # v0.11.25：默认保护——批次含非 pending 或已预标 task 时拒删，需显式 force（前端弹保护框）。
        if not force:
            non_pending, predicted, affected = await self._count_protected_tasks(
                batch_id
            )
            if affected:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "batch_has_active_work",
                        "requires_force": True,
                        "non_pending": non_pending,
                        "predicted": predicted,
                        "affected_tasks": affected,
                        "message": (
                            "批次含进行中/已完成或 AI 预标过的任务；删除将重置为待标注"
                            "并清除 AI 预标。请改用归档，或确认强制删除。"
                        ),
                    },
                )

        # v0.11.23：解绑前先重置非 pending task 为 pending、清 AI 预标（保留人工标注），
        # 否则 review/completed task 解绑后成孤儿、AI 预标残留会在重分包再预标时叠加重复标注。
        # 必须在改写 batch_id 之前调用（清理靠 Task.batch_id==batch_id 子查询定位）。
        await self._reset_and_clean_batch_tasks(batch_id, actor_id=actor_id)

        # v0.6.8 B-14：老项目仍走「回收到 B-DEFAULT」路径；新项目无 B-DEFAULT 时把任务回退为
        # batch_id=NULL（成为「未归类任务」），由 split 流程兜底，避免删完所有批次后死锁。
        default = await self.get_default_batch(batch.project_id)
        if default:
            await self.db.execute(
                update(Task)
                .where(Task.batch_id == batch_id)
                .values(batch_id=default.id)
            )
            await self.recalculate_counters(default.id)
        else:
            await self.db.execute(
                update(Task).where(Task.batch_id == batch_id).values(batch_id=None)
            )
            # 无 B-DEFAULT 兜底时没有 recalculate_counters 触发 project 计数同步，
            # 这里显式刷一次，否则强删含进行中/已完成 task 的批次后 project 物化列会停在旧快照。
            await self._sync_project_counters(batch.project_id)

        await self.db.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
        await self.db.flush()
        return True

    # ── Split strategies ───────────────────────────────────────────────────

    async def split(
        self,
        project_id: uuid.UUID,
        data: BatchSplitRequest,
        created_by: uuid.UUID,
    ) -> list[TaskBatch]:
        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if data.annotator_id is not None:
            assignment_targets.append(("annotator", data.annotator_id))
        if data.reviewer_id is not None:
            assignment_targets.append(("reviewer", data.reviewer_id))
        await self._lock_and_validate_assignment_targets(project_id, assignment_targets)
        # scene 模式项目分包只能按 scene:同一 scene 的连续帧必须落同一批次,random/顺序
        # 切分会把连续帧拆散给不同标注员。前端已只暴露 by_scene 入口,这里再加一道后端门
        # 防御直发 API 的越权策略。
        project = await self.db.get(Project, project_id)
        if project and project.scene_mode and data.strategy != "by_scene":
            raise HTTPException(
                status_code=422,
                detail="scene 模式项目分包只能按 scene(strategy=by_scene)",
            )
        if data.strategy == "random":
            return await self._split_random(project_id, data, created_by)
        elif data.strategy == "metadata":
            batches = await self._split_metadata(project_id, data, created_by)
            return [batches]
        elif data.strategy == "id_range":
            batches = await self._split_by_ids(project_id, data, created_by)
            return [batches]
        elif data.strategy == "by_scene":
            return await self._split_by_scene(project_id, data, created_by)
        raise HTTPException(
            status_code=400, detail=f"Unknown strategy: {data.strategy}"
        )

    async def _split_random(
        self,
        project_id: uuid.UUID,
        data: BatchSplitRequest,
        created_by: uuid.UUID,
    ) -> list[TaskBatch]:
        n = data.n_batches
        if not n:
            raise HTTPException(
                status_code=400, detail="n_batches is required for random strategy"
            )

        default = await self.get_default_batch(project_id)
        task_ids = await self._splittable_task_ids(project_id, default)
        if not task_ids:
            raise HTTPException(status_code=400, detail="No unassigned tasks to split")

        # v0.11.22：shuffle=False 时保留 _splittable_task_ids 的创建顺序（顺序切分 / 顺序注入）。
        if data.shuffle:
            random.shuffle(task_ids)
        chunk_size = len(task_ids) // n
        remainder = len(task_ids) % n

        batches: list[TaskBatch] = []
        offset = 0
        for i in range(n):
            size = chunk_size + (1 if i < remainder else 0)
            chunk = task_ids[offset : offset + size]
            offset += size

            batch = TaskBatch(
                project_id=project_id,
                display_id=await next_display_id(self.db, "batches"),
                # v0.11.22：n=1（把全部未归类任务注入一个批次）时直接用名称本身，不加 " 1" 后缀。
                name=data.name_prefix if n == 1 else f"{data.name_prefix} {i + 1}",
                status=BatchStatus.DRAFT,
                priority=data.priority,
                deadline=data.deadline,
                annotator_id=data.annotator_id,
                reviewer_id=data.reviewer_id,
                created_by=created_by,
            )
            self._sync_assigned_user_ids(batch)
            self.db.add(batch)
            await self.db.flush()

            await self._assign_tasks(batch.id, chunk)
            await self.recalculate_counters(batch.id)
            batches.append(batch)

        if default is not None:
            await self.recalculate_counters(default.id)
        return batches

    async def _split_metadata(
        self,
        project_id: uuid.UUID,
        data: BatchSplitRequest,
        created_by: uuid.UUID,
    ) -> TaskBatch:
        if not data.metadata_key or data.metadata_value is None:
            raise HTTPException(
                status_code=400, detail="metadata_key and metadata_value are required"
            )

        default = await self.get_default_batch(project_id)
        # v0.6.8 B-14：metadata 过滤同样作用于「未归类 ∪ B-DEFAULT」任务集合
        conds = [Task.batch_id.is_(None)]
        if default is not None:
            conds.append(Task.batch_id == default.id)
        result = await self.db.execute(
            select(Task.id)
            .join(DatasetItem, Task.dataset_item_id == DatasetItem.id)
            .where(
                Task.project_id == project_id,
                or_(*conds),
                DatasetItem.metadata_[data.metadata_key].astext == data.metadata_value,
            )
        )
        task_ids = [row[0] for row in result.fetchall()]
        if not task_ids:
            raise HTTPException(
                status_code=400, detail="No tasks match the metadata filter"
            )

        batch = TaskBatch(
            project_id=project_id,
            display_id=await next_display_id(self.db, "batches"),
            name=f"{data.name_prefix} ({data.metadata_key}={data.metadata_value})",
            status=BatchStatus.DRAFT,
            priority=data.priority,
            deadline=data.deadline,
            annotator_id=data.annotator_id,
            reviewer_id=data.reviewer_id,
            created_by=created_by,
        )
        self._sync_assigned_user_ids(batch)
        self.db.add(batch)
        await self.db.flush()

        await self._assign_tasks(batch.id, task_ids)
        await self.recalculate_counters(batch.id)
        if default is not None:
            await self.recalculate_counters(default.id)
        return batch

    async def _split_by_ids(
        self,
        project_id: uuid.UUID,
        data: BatchSplitRequest,
        created_by: uuid.UUID,
    ) -> TaskBatch:
        if not data.item_ids:
            raise HTTPException(
                status_code=400, detail="item_ids is required for id_range strategy"
            )

        default = await self.get_default_batch(project_id)
        # v0.6.8 B-14：id_range 同样作用于「未归类 ∪ B-DEFAULT」任务集合
        conds = [Task.batch_id.is_(None)]
        if default is not None:
            conds.append(Task.batch_id == default.id)
        result = await self.db.execute(
            select(Task.id).where(
                Task.project_id == project_id,
                or_(*conds),
                Task.dataset_item_id.in_(data.item_ids),
            )
        )
        task_ids = [row[0] for row in result.fetchall()]
        if not task_ids:
            raise HTTPException(
                status_code=400, detail="No tasks match the provided item IDs"
            )

        batch = TaskBatch(
            project_id=project_id,
            display_id=await next_display_id(self.db, "batches"),
            name=data.name_prefix,
            status=BatchStatus.DRAFT,
            priority=data.priority,
            deadline=data.deadline,
            annotator_id=data.annotator_id,
            reviewer_id=data.reviewer_id,
            created_by=created_by,
        )
        self._sync_assigned_user_ids(batch)
        self.db.add(batch)
        await self.db.flush()

        await self._assign_tasks(batch.id, task_ids)
        await self.recalculate_counters(batch.id)
        if default is not None:
            await self.recalculate_counters(default.id)
        return batch

    async def _split_by_scene(
        self,
        project_id: uuid.UUID,
        data: BatchSplitRequest,
        created_by: uuid.UUID,
    ) -> list[TaskBatch]:
        default = await self.get_default_batch(project_id)
        task_ids = await self._splittable_task_ids(project_id, default)
        if not task_ids:
            raise HTTPException(status_code=400, detail="No unassigned tasks to split")

        scene_frames = await resolve_task_scene_frames(self.db, task_ids)
        grouped: dict[uuid.UUID | None, list[uuid.UUID]] = {}
        scene_names: dict[uuid.UUID | None, str] = {}
        original_order = {task_id: i for i, task_id in enumerate(task_ids)}
        for task_id in task_ids:
            info = scene_frames.get(task_id)
            scene_id = info.scene_id if info else None
            grouped.setdefault(scene_id, []).append(task_id)
            if scene_id is not None and info and info.scene_name:
                scene_names[scene_id] = info.scene_name

        def _group_sort_key(scene_id: uuid.UUID | None) -> tuple[int, str]:
            if scene_id is None:
                return (1, "")
            return (0, scene_names.get(scene_id) or str(scene_id))

        batches: list[TaskBatch] = []
        for scene_id in sorted(grouped, key=_group_sort_key):
            chunk = grouped[scene_id]
            chunk.sort(
                key=lambda task_id: (
                    scene_frames[task_id].frame_index is None,
                    scene_frames[task_id].frame_index
                    if scene_frames[task_id].frame_index is not None
                    else original_order[task_id],
                    original_order[task_id],
                )
            )

            scene_name = scene_names.get(scene_id) if scene_id is not None else None
            batch_name = (
                f"{data.name_prefix} · {scene_name}"
                if scene_name
                else f"{data.name_prefix} · 无 scene"
            )
            batch = TaskBatch(
                project_id=project_id,
                display_id=await next_display_id(self.db, "batches"),
                name=batch_name[:255],
                status=BatchStatus.DRAFT,
                priority=data.priority,
                deadline=data.deadline,
                annotator_id=data.annotator_id,
                reviewer_id=data.reviewer_id,
                created_by=created_by,
            )
            self._sync_assigned_user_ids(batch)
            self.db.add(batch)
            await self.db.flush()

            await self._assign_tasks(batch.id, chunk)
            await self._set_sequence_orders(
                {
                    task_id: scene_frames[task_id].frame_index
                    for task_id in chunk
                    if scene_frames[task_id].frame_index is not None
                }
            )
            await self.recalculate_counters(batch.id)
            batches.append(batch)

        if default is not None:
            await self.recalculate_counters(default.id)
        return batches

    # ── Task assignment ────────────────────────────────────────────────────

    async def _assign_tasks(
        self, batch_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> int:
        if not task_ids:
            return 0
        # v0.12.0 B5 · 分块 UPDATE，避免十万级未归类「一键全量建包」打成单条巨 IN/UPDATE。
        for i in range(0, len(task_ids), ASSIGN_CHUNK_SIZE):
            chunk = task_ids[i : i + ASSIGN_CHUNK_SIZE]
            await self.db.execute(
                update(Task).where(Task.id.in_(chunk)).values(batch_id=batch_id)
            )
        return len(task_ids)

    async def _set_sequence_orders(
        self, frame_by_task: dict[uuid.UUID, int | None]
    ) -> None:
        concrete = {
            task_id: frame_index
            for task_id, frame_index in frame_by_task.items()
            if frame_index is not None
        }
        if not concrete:
            return
        task_ids = list(concrete)
        for i in range(0, len(task_ids), ASSIGN_CHUNK_SIZE):
            chunk = task_ids[i : i + ASSIGN_CHUNK_SIZE]
            await self.db.execute(
                update(Task)
                .where(Task.id.in_(chunk))
                .values(
                    sequence_order=case(
                        {task_id: concrete[task_id] for task_id in chunk},
                        value=Task.id,
                    )
                )
            )

    async def assign_tasks_to_batch(
        self, batch_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> int:
        count = await self._assign_tasks(batch_id, task_ids)
        await self.recalculate_counters(batch_id)
        return count

    async def distribute_batches_in_project(
        self,
        project_id: uuid.UUID,
        *,
        annotator_ids: list[uuid.UUID],
        reviewer_ids: list[uuid.UUID],
        only_unassigned: bool = True,
        preview_version: str | None = None,
        batch_ids: list[uuid.UUID] | None = None,
    ) -> dict[str, Any]:
        """v0.7.2：把项目下的 batch 圆周分派给所选 annotator / reviewer。
        - 一 batch = 一标注员 + 一审核员
        - only_unassigned=True：仅 annotator_id IS NULL 的 batch 写 annotator；reviewer 同理
        - 不会处理 archived 状态的 batch
        - 同时回填 batch 下所有 task 的 assignee_id / reviewer_id
        """
        from app.services.management import preview_batch_distribution

        plan = await preview_batch_distribution(
            self.db,
            project_id=project_id,
            annotator_ids=annotator_ids,
            reviewer_ids=reviewer_ids,
            only_unassigned=only_unassigned,
            validate_targets=self._lock_and_validate_assignment_targets,
            lock_batches=True,
            batch_ids=batch_ids,
        )
        if preview_version is not None and preview_version != plan.preview_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "distribution_preview_stale",
                    "message": "批次分派或任务负载已变化，请重新预览后确认",
                },
            )

        # Keep the exact locked batch set used to compute the reviewed plan.
        batches = (
            (
                await self.db.execute(
                    select(TaskBatch).where(
                        TaskBatch.id.in_([item.batch_id for item in plan.items])
                    )
                )
            )
            .scalars()
            .all()
        )
        batches_by_id = {batch.id: batch for batch in batches}
        annotator_per_batch: dict[str, str | None] = {}
        reviewer_per_batch: dict[str, str | None] = {}
        affected = 0
        changed_ids: list[uuid.UUID] = []
        for item in plan.items:
            b = batches_by_id[item.batch_id]
            if item.before_annotator_id != item.after_annotator_id:
                b.annotator_id = item.after_annotator_id
                await self._cascade_task_assignee(b.id, b.annotator_id)
            if item.before_reviewer_id != item.after_reviewer_id:
                b.reviewer_id = item.after_reviewer_id
                await self._cascade_task_reviewer(b.id, b.reviewer_id)
            annotator_per_batch[str(b.id)] = (
                str(b.annotator_id) if b.annotator_id else None
            )
            reviewer_per_batch[str(b.id)] = (
                str(b.reviewer_id) if b.reviewer_id else None
            )
            if item.will_change:
                self._sync_assigned_user_ids(b)
                affected += 1
                changed_ids.append(b.id)

        await self.db.flush()

        # issue #124 · 分派不改变 batch.status, 单独广播让可预标列表实时收敛。
        if changed_ids:
            await publish_batch_assignment_change(
                str(project_id), [str(b) for b in changed_ids]
            )

        return {
            "distributed_batches": affected,
            "annotator_per_batch": annotator_per_batch,
            "reviewer_per_batch": reviewer_per_batch,
        }

    # ── Counters ───────────────────────────────────────────────────────────

    async def recalculate_counters(self, batch_id: uuid.UUID) -> None:
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            return

        result = await self.db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Task.status == "completed").label("completed"),
                func.count().filter(Task.status == "review").label("review"),
            ).where(Task.batch_id == batch_id)
        )
        row = result.one()
        batch.total_tasks = row.total
        batch.completed_tasks = row.completed
        batch.review_tasks = row.review

        await self.db.flush()
        await self._sync_project_counters(batch.project_id)

    async def _sync_project_counters(self, project_id: uuid.UUID) -> None:
        project = await self.db.get(Project, project_id)
        if not project:
            return

        result = await self.db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Task.status == "completed").label("completed"),
                func.count().filter(Task.status == "review").label("review"),
                func.count().filter(Task.status == "in_progress").label("in_progress"),
            ).where(Task.project_id == project_id)
        )
        row = result.one()
        project.total_tasks = row.total
        project.completed_tasks = row.completed
        project.review_tasks = row.review
        project.in_progress_tasks = row.in_progress

        # v0.10.25 · 同步 batch_summary 物化列 (取代 list_projects 实时 GROUP BY)
        bs_row = (
            await self.db.execute(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter(TaskBatch.annotator_id.is_not(None))
                    .label("assigned"),
                    func.count()
                    .filter(TaskBatch.status == "reviewing")
                    .label("in_review"),
                ).where(TaskBatch.project_id == project_id)
            )
        ).one()
        project.batch_summary = {
            "total": int(bs_row.total),
            "assigned": int(bs_row.assigned),
            "in_review": int(bs_row.in_review),
        }
        await self.db.flush()

    # ── Auto-transitions ───────────────────────────────────────────────────

    async def check_auto_transitions(self, batch_id: uuid.UUID | None) -> None:
        # v0.9.12 · INFO 日志诊断 B-15: admin 反馈 "标注员开始标注后 batch 未转 annotating".
        # 6 个调用点见 tasks.py (acquire_next_task / save_annotation / complete_task etc.).
        if not batch_id:
            return
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            logger.info("check_auto_transitions: batch_id=%s not found", batch_id)
            return
        # v0.9.15 · ADR-0008: admin_locked batch 跳过自动状态推进
        if batch.admin_locked:
            logger.info(
                "check_auto_transitions: batch_id=%s admin_locked, skipping", batch_id
            )
            return

        from_status = batch.status

        if batch.status in (BatchStatus.ACTIVE, BatchStatus.PRE_ANNOTATED):
            has_in_progress = await self.db.execute(
                select(Task.id)
                .where(
                    Task.batch_id == batch_id,
                    # M1: rejected 也算"标注进行中"
                    Task.status.in_(["in_progress", "rejected"]),
                )
                .limit(1)
            )
            if has_in_progress.scalar_one_or_none():
                batch.status = BatchStatus.ANNOTATING
                await self.db.flush()
                logger.info(
                    "batch.auto_transition: %s %s -> annotating (task in_progress detected)",
                    batch_id,
                    from_status,
                )
                await publish_batch_status_change(
                    str(batch.project_id),
                    str(batch_id),
                    from_status,
                    BatchStatus.ANNOTATING,
                )
            else:
                logger.debug(
                    "batch.auto_transition: %s %s -> no_change (no in_progress task)",
                    batch_id,
                    from_status,
                )

        elif batch.status == BatchStatus.ANNOTATING:
            pending_or_ip = await self.db.execute(
                select(Task.id)
                .where(
                    Task.batch_id == batch_id,
                    # M1: rejected 任务仍需标注员重做，不能视为完成
                    Task.status.in_(["pending", "in_progress", "rejected"]),
                )
                .limit(1)
            )
            if not pending_or_ip.scalar_one_or_none():
                batch.status = BatchStatus.REVIEWING
                await self.db.flush()
                logger.info(
                    "batch.auto_transition: %s annotating -> reviewing (all tasks done)",
                    batch_id,
                )
                await publish_batch_status_change(
                    str(batch.project_id),
                    str(batch_id),
                    from_status,
                    BatchStatus.REVIEWING,
                )

    # ── Batch rejection ────────────────────────────────────────────────────

    async def _lock_decision_scope(
        self,
        actor_id: uuid.UUID | None,
        batch_ids: list[uuid.UUID],
        *,
        statuses: tuple[str, ...] = ("review", "completed"),
        project_id: uuid.UUID | None = None,
    ) -> dict[uuid.UUID, TaskBatch]:
        """Lock actor, membership, batches and affected tasks before mutation.

        Account-first: the actor row (``assert_task_user_active``) and the
        project membership (``FOR SHARE``) are locked, then the batches
        (``FOR UPDATE NOWAIT``), then the affected Task rows in stable id order
        with bounded ``NOWAIT``.  A busy batch/task rolls back as a retryable
        409 *before* any mutation, so callers never continue after a global
        rollback and a failing item is never partially mutated.  ``actor_id=None``
        fails closed.
        """

        from sqlalchemy.exc import DBAPIError

        from app.db.models.project import Project
        from app.db.models.user import User
        from app.services.annotation_evidence import lock_tasks_for_evidence
        from app.services.project_access import (
            ProjectCapability,
            resolve_project_access,
        )
        from app.services.task_lock import assert_task_user_active

        if actor_id is None:
            raise HTTPException(
                status_code=403,
                detail={"reason": "review_evidence_actor_required"},
            )
        if not batch_ids:
            return {}
        if not await assert_task_user_active(self.db, actor_id):
            raise HTTPException(status_code=401, detail="账号已停用")
        actor = await self.db.get(User, actor_id, populate_existing=True)
        if actor is None:
            raise HTTPException(status_code=401, detail="账号已停用")

        project_ids_stmt = (
            select(TaskBatch.project_id).where(TaskBatch.id.in_(batch_ids)).distinct()
        )
        if project_id is not None:
            project_ids_stmt = project_ids_stmt.where(
                TaskBatch.project_id == project_id
            )
        project_ids = sorted((await self.db.execute(project_ids_stmt)).scalars().all())
        if project_ids:
            # Lock the project authority rows before authorizing: an ownership
            # transfer takes the same rows FOR UPDATE, so a transfer committing
            # mid-decision cannot strand an unauthorized actor inside batch and
            # task locks.  Lock order stays account -> project -> membership ->
            # batches -> tasks, matching the lifecycle model.
            try:
                await self.db.execute(
                    select(Project.id)
                    .where(Project.id.in_(project_ids))
                    .order_by(Project.id)
                    .with_for_update(nowait=True)
                )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                    await self.db.rollback()
                    raise HTTPException(
                        status_code=409, detail={"reason": "batch_decision_busy"}
                    ) from exc
                raise
        for scope_project_id in project_ids:
            project = await self.db.get(
                Project, scope_project_id, populate_existing=True
            )
            if project is None:
                raise HTTPException(status_code=404, detail="Batch not found")
            access = await resolve_project_access(
                self.db, user=actor, project=project, lock_membership=True
            )
            if not (
                access.is_manager or access.has(ProjectCapability.REVIEW_WRITE.value)
            ):
                raise HTTPException(
                    status_code=403,
                    detail={"reason": "review_capability_required"},
                )

        batch_stmt = select(TaskBatch).where(TaskBatch.id.in_(batch_ids))
        if project_id is not None:
            batch_stmt = batch_stmt.where(TaskBatch.project_id == project_id)
        try:
            rows = (
                (
                    await self.db.execute(
                        batch_stmt.order_by(TaskBatch.id)
                        .with_for_update(nowait=True)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                await self.db.rollback()
                raise HTTPException(
                    status_code=409, detail={"reason": "batch_decision_busy"}
                ) from exc
            raise

        locked_ids = [batch.id for batch in rows]
        task_ids = list(
            (
                await self.db.execute(
                    select(Task.id)
                    .where(
                        Task.batch_id.in_(locked_ids),
                        Task.status.in_(list(statuses)),
                    )
                    .order_by(Task.id)
                )
            ).scalars()
        )
        await lock_tasks_for_evidence(self.db, task_ids, nowait=True)
        return {batch.id: batch for batch in rows}

    async def _assert_locked_batch_evidence(
        self,
        batch: TaskBatch,
        actor_id: uuid.UUID | None,
        *,
        statuses: tuple[str, ...] = ("review", "completed"),
    ) -> None:
        """Validate frozen non-self evidence on already-locked Task rows."""

        from app.services.annotation_evidence import assert_review_evidence_current

        rows = (
            (
                await self.db.execute(
                    select(Task)
                    .where(Task.batch_id == batch.id, Task.status.in_(list(statuses)))
                    .order_by(Task.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for task in rows:
            effective_annotator_id = (
                task.assignee_id if task.assignee_id is not None else batch.annotator_id
            )
            assert_review_evidence_current(
                task, actor_id, effective_annotator_id=effective_annotator_id
            )

    async def reject_batch(
        self,
        batch_id: uuid.UUID,
        *,
        feedback: str,
        reviewer_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> tuple[TaskBatch, int]:
        """v0.7.0 方案 A · 软重置语义：
        - 仅把 review/completed 任务回退到 pending（让标注员可继续动它们）
        - **不**改 is_labeled，**不**清 annotations.is_active（保留历史标注）
        - 批次写入 review_feedback / reviewed_at / reviewed_by
        """
        locked = await self._lock_decision_scope(actor_id or reviewer_id, [batch_id])
        batch = locked.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        # v0.24.x：整批退回允许从 active / annotating / reviewing 发起——审核页会列出
        # 部分送审（review_tasks > 0）但批次状态尚未推进到 reviewing 的批次，此时也应
        # 能把整批打回重做。语义仍是软重置：review/completed → pending，写反馈并置 rejected。
        rejectable = {
            BatchStatus.ACTIVE,
            BatchStatus.ANNOTATING,
            BatchStatus.REVIEWING,
        }
        if batch.status not in rejectable:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reject batch in status '{batch.status}'",
            )

        await self._assert_locked_batch_evidence(batch, actor_id or reviewer_id)
        result = await self.db.execute(
            update(Task)
            .where(
                Task.batch_id == batch_id,
                Task.status.in_(["review", "completed"]),
            )
            .values(
                status="pending",
                review_contributor_ids=None,
                review_submitter_id=None,
            )
        )
        affected = result.rowcount

        batch.status = BatchStatus.REJECTED
        batch.review_feedback = feedback
        batch.reviewed_at = datetime.now(timezone.utc)
        batch.reviewed_by = reviewer_id
        await self.db.flush()
        await self.recalculate_counters(batch_id)
        return batch, affected

    # ── Reset to draft (v0.7.6) ────────────────────────────────────────────

    async def reset_to_draft(
        self, batch_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> tuple[TaskBatch, int, dict[str, int]]:
        """v0.7.6 · 终极重置：任意状态 → draft.

        v0.9.12 BUG B-15 · 加 prediction 级联清理 (predictions / failed_predictions /
        prediction_jobs / prediction_metas), 否则 `/ai-pre` 仍渲染该 batch 卡片.

        B-33 (v0.10.17) · 之前只删 prediction 行, 却漏改 task 上的物化字段
        (total_predictions / total_annotations / is_labeled) 与 AI 已采纳的 annotation,
        导致 reset 之后 task 仍标 "AI 已预标"; 更严重的是再次跑预标时新 prediction
        会与残留的 prediction_based annotation 叠加. 修复:
          - 软删 source='prediction_based' 的 annotation (人工标注 source='manual' 保留)
          - task.total_predictions 归 0; 重算 total_annotations / is_labeled
        删除顺序: NULL annotations.parent_prediction_id → DELETE prediction_metas →
        DELETE predictions/failed_predictions → DELETE prediction_jobs.

        Returns: (batch, tasks_reset, cascade_counts)
            cascade_counts = {"predictions": N, "failed_predictions": N,
                              "prediction_jobs": N, "ai_annotations_deactivated": N}
        """
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        counts = await self._reset_and_clean_batch_tasks(batch_id, actor_id=actor_id)
        affected = counts.pop("tasks_reset")

        batch.status = BatchStatus.DRAFT
        batch.review_feedback = None
        batch.reviewed_at = None
        batch.reviewed_by = None

        await self.db.flush()
        await self.recalculate_counters(batch_id)
        return batch, affected, counts

    async def _reset_and_clean_batch_tasks(
        self, batch_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> dict[str, int]:
        """把批次内所有非 pending task 重置为 pending, 并清理 AI 预标产物, 保留人工标注.

        供 reset_to_draft (v0.7.6) 与 delete / bulk_delete (v0.11.23) 复用. 删除批次若
        不走这套清理, review/completed 的 task 会带状态变孤儿; AI 预标残留 (predictions
        + prediction_based annotation) 会在重分包后再次预标时叠加重复标注 (见 B-33).

        必须在 task 仍 batch_id==batch_id 时调用 (状态重置/锁/job 清理靠该条件定位).
        预测清理委托给 clean_task_predictions (按 task_id).

        Returns: cascade_counts (含 tasks_reset / predictions / failed_predictions /
                 prediction_jobs / ai_annotations_deactivated).
        """
        from app.db.models.project_member import ProjectMember
        from app.db.models.task_lock import TaskLock
        from app.db.models.async_job import AsyncJob

        result = await self.db.execute(
            update(Task)
            .where(
                Task.batch_id == batch_id,
                Task.status != "pending",
            )
            .values(
                status="pending",
                review_contributor_ids=None,
                review_submitter_id=None,
            )
        )
        tasks_reset = result.rowcount

        # Role-change handoffs materialize the old batch default onto terminal
        # history (non-override explicit columns) to freeze attribution.  This
        # reset reactivates those rows as pending work, where a stale explicit
        # assignment would keep overriding the batch default with a user who no
        # longer holds the matching project role, leaving the task unworkable.
        # Restore inheritance for exactly those rows; genuine manual overrides
        # (override flags) and current members keep their assignment.
        for role_value, column, override_column, extra_values in (
            ("annotator", Task.assignee_id, Task.assignee_is_override, {}),
            (
                "reviewer",
                Task.reviewer_id,
                Task.reviewer_is_override,
                {"reviewer_claimed_at": None},
            ),
        ):
            await self.db.execute(
                update(Task)
                .where(
                    Task.batch_id == batch_id,
                    Task.status == "pending",
                    column.is_not(None),
                    override_column.is_(False),
                    ~select(ProjectMember.id)
                    .where(
                        ProjectMember.project_id == Task.project_id,
                        ProjectMember.user_id == column,
                        ProjectMember.role == role_value,
                    )
                    .exists(),
                )
                .values({column.key: None, **extra_values})
            )

        await self.db.execute(
            delete(TaskLock).where(
                TaskLock.task_id.in_(select(Task.id).where(Task.batch_id == batch_id))
            )
        )

        # v0.10.49 · prediction_jobs 已收敛进 async_jobs；按 payload.batch_id 清本批 job 历史
        job_result = await self.db.execute(
            delete(AsyncJob).where(
                AsyncJob.kind == "batch_predict",
                AsyncJob.payload["batch_id"].astext == str(batch_id),
            )
        )

        task_ids = [
            r[0]
            for r in (
                await self.db.execute(select(Task.id).where(Task.batch_id == batch_id))
            ).all()
        ]
        pred_counts = await self.clean_task_predictions(task_ids, actor_id=actor_id)

        return {
            "tasks_reset": tasks_reset,
            "prediction_jobs": job_result.rowcount or 0,
            **pred_counts,
        }

    async def clean_task_predictions(
        self,
        task_ids: list[uuid.UUID],
        *,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """按 task_id 清理 AI 预标产物 (不动 task 状态 / 锁 / job): 软删 prediction_based
        annotation (保留 source='manual')、NULL parent_prediction_id、删 prediction_metas
        / predictions / failed_predictions、total_predictions 归 0 并按存活人工标注重算
        total_annotations / is_labeled.

        供 _reset_and_clean_batch_tasks (删批次/重置) 与 batch_predict overwrite 模式
        (v0.11.24) 复用. task_ids 为空时 no-op.

        A2 · 软删 AI 标注是内容删除, 需要证据: 用户触发的调用传入 ``actor_id`` 并记为该
        actor 的贡献; 系统触发且无 actor 时, 已记录的贡献会漏掉删除者, 因此把这些 task 的
        累积集合置回 NULL(未知), 不冒充 owner。``parent_prediction_id`` 清理只是元数据,
        不算人工编辑, 不记贡献。

        Returns: {"predictions": N, "failed_predictions": N, "ai_annotations_deactivated": N}
        """
        from app.db.models.prediction import (
            Prediction,
            FailedPrediction,
            PredictionMeta,
        )
        from app.db.models.annotation import Annotation
        from app.services.annotation_evidence import (
            lock_tasks_for_evidence,
            record_annotation_actors_for_tasks,
        )
        from sqlalchemy import text, bindparam

        if not task_ids:
            return {
                "predictions": 0,
                "failed_predictions": 0,
                "ai_annotations_deactivated": 0,
            }

        # Serialize the content snapshot with producers before determining which
        # tasks lose AI content. Multi-batch callers can already hold other locks.
        await lock_tasks_for_evidence(self.db, task_ids, nowait=True)

        # A2 · capture which tasks actually lose accepted AI content, and record
        # the deletion actor before the soft-delete. A system cleanup without an
        # attributable actor conservatively drops known evidence to unknown.
        ai_affected_task_ids = list(
            (
                await self.db.execute(
                    select(Annotation.task_id)
                    .where(
                        Annotation.task_id.in_(task_ids),
                        Annotation.source == "prediction_based",
                        Annotation.is_active.is_(True),
                    )
                    .distinct()
                )
            ).scalars()
        )
        if ai_affected_task_ids:
            if actor_id is not None:
                await record_annotation_actors_for_tasks(
                    self.db, ai_affected_task_ids, actor_id
                )
            else:
                await self.db.execute(
                    update(Task)
                    .where(Task.id.in_(ai_affected_task_ids))
                    .values(annotation_contributor_ids=None)
                )

        # B-33 · 软删 AI 采纳的 annotation. 保留 source='manual' 的人工标注 (不丢工作量).
        ai_anno_result = await self.db.execute(
            update(Annotation)
            .where(
                Annotation.task_id.in_(task_ids),
                Annotation.source == "prediction_based",
                Annotation.is_active.is_(True),
            )
            .values(is_active=False)
        )

        # annotations.parent_prediction_id FK 无 ondelete; 必须先 NULL 才能 DELETE predictions
        await self.db.execute(
            update(Annotation)
            .where(Annotation.task_id.in_(task_ids))
            .values(parent_prediction_id=None)
        )

        # prediction_metas FK 无 ondelete; 先删
        pred_ids = select(Prediction.id).where(Prediction.task_id.in_(task_ids))
        failed_ids = select(FailedPrediction.id).where(
            FailedPrediction.task_id.in_(task_ids)
        )
        await self.db.execute(
            delete(PredictionMeta).where(
                or_(
                    PredictionMeta.prediction_id.in_(pred_ids),
                    PredictionMeta.failed_prediction_id.in_(failed_ids),
                )
            )
        )

        pred_result = await self.db.execute(
            delete(Prediction).where(Prediction.task_id.in_(task_ids))
        )
        failed_result = await self.db.execute(
            delete(FailedPrediction).where(FailedPrediction.task_id.in_(task_ids))
        )

        # B-33 · 同步 task 物化字段: total_predictions 归 0, total_annotations / is_labeled
        # 按剩余 active+未取消的 annotation 重算. 一条 UPDATE+子查询, 按 task_id 定位.
        stmt = text(
            """
            UPDATE tasks AS t
            SET total_predictions = 0,
                total_annotations = COALESCE(sub.cnt, 0),
                is_labeled = COALESCE(sub.cnt, 0) > 0
            FROM (
                SELECT t2.id AS task_id, (
                    SELECT COUNT(*) FROM annotations a
                    WHERE a.task_id = t2.id
                      AND a.is_active = TRUE
                      AND a.was_cancelled = FALSE
                ) AS cnt
                FROM tasks t2
                WHERE t2.id IN :tids
            ) AS sub
            WHERE t.id = sub.task_id
            """
        ).bindparams(bindparam("tids", expanding=True))
        await self.db.execute(stmt, {"tids": task_ids})

        return {
            "predictions": pred_result.rowcount or 0,
            "failed_predictions": failed_result.rowcount or 0,
            "ai_annotations_deactivated": ai_anno_result.rowcount or 0,
        }

    # ── Bulk operations (v0.7.3) ───────────────────────────────────────────

    async def _list_batches_in_project(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
        *,
        for_update: bool = False,
    ) -> dict[uuid.UUID, TaskBatch]:
        if not batch_ids:
            return {}
        stmt = (
            select(TaskBatch)
            .where(
                TaskBatch.project_id == project_id,
                TaskBatch.id.in_(batch_ids),
            )
            .order_by(TaskBatch.id)
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt)
        return {b.id: b for b in result.scalars().all()}

    async def bulk_archive(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
    ) -> dict[str, list[dict]]:
        """逐个 transition → archived。已 archived 算 skipped。
        语法层不允许的迁移（如 draft → archived）目前 VALID_TRANSITIONS 不收，会回 failed。"""
        loaded = await self._list_batches_in_project(project_id, batch_ids)
        succeeded: list[uuid.UUID] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if batch.status == BatchStatus.ARCHIVED:
                skipped.append({"batch_id": bid, "reason": "already archived"})
                continue
            allowed = VALID_TRANSITIONS.get(batch.status, set())
            if BatchStatus.ARCHIVED not in allowed:
                failed.append(
                    {"batch_id": bid, "reason": f"cannot archive from '{batch.status}'"}
                )
                continue
            batch.status = BatchStatus.ARCHIVED
            succeeded.append(bid)
        await self.db.flush()
        return {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    async def bulk_delete(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
        *,
        force: bool = False,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, list[dict]]:
        loaded = await self._list_batches_in_project(project_id, batch_ids)
        succeeded: list[uuid.UUID] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        default = await self.get_default_batch(project_id)
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if batch.display_id == "B-DEFAULT":
                skipped.append(
                    {"batch_id": bid, "reason": "B-DEFAULT cannot be deleted"}
                )
                continue
            # v0.11.25：默认保护——含进行中成果/已预标的批次非 force 时计入 failed（partial 语义）
            if not force:
                non_pending, predicted, affected = await self._count_protected_tasks(
                    bid
                )
                if affected:
                    failed.append(
                        {
                            "batch_id": bid,
                            "reason": "requires_force",
                            "non_pending": non_pending,
                            "predicted": predicted,
                            "affected_tasks": affected,
                        }
                    )
                    continue
            # v0.11.23：与单删一致——解绑前先重置非 pending task + 清 AI 预标（保留人工标注）
            await self._reset_and_clean_batch_tasks(bid, actor_id=actor_id)
            # 复用单个删除路径里的 task 接管逻辑（按 default 是否存在二选一）
            if default is not None:
                await self.db.execute(
                    update(Task).where(Task.batch_id == bid).values(batch_id=default.id)
                )
            else:
                await self.db.execute(
                    update(Task).where(Task.batch_id == bid).values(batch_id=None)
                )
            await self.db.execute(delete(TaskBatch).where(TaskBatch.id == bid))
            succeeded.append(bid)
        await self.db.flush()
        if default is not None and succeeded:
            await self.recalculate_counters(default.id)
        elif succeeded:
            # 无 B-DEFAULT 时缺少 recalculate_counters 兜底，显式同步 project 物化列
            await self._sync_project_counters(project_id)
        return {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    async def bulk_reassign(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
        *,
        annotator_id: uuid.UUID | None,
        reviewer_id: uuid.UUID | None,
        annotator_set: bool,
        reviewer_set: bool,
    ) -> dict[str, list[dict]]:
        """单事务原子改派。annotator_set/reviewer_set 为 True 时表示该字段需要更新（值可以是 None 表示清空）。"""
        if not annotator_set and not reviewer_set:
            raise HTTPException(
                status_code=400, detail="annotator_id or reviewer_id required"
            )
        assignment_targets: list[tuple[str, uuid.UUID]] = []
        if annotator_set and annotator_id is not None:
            assignment_targets.append(("annotator", annotator_id))
        if reviewer_set and reviewer_id is not None:
            assignment_targets.append(("reviewer", reviewer_id))
        await self._lock_and_validate_assignment_targets(project_id, assignment_targets)
        loaded = await self._list_batches_in_project(
            project_id, batch_ids, for_update=True
        )
        succeeded: list[uuid.UUID] = []
        failed: list[dict] = []
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if annotator_set:
                batch.annotator_id = annotator_id
                await self._cascade_task_assignee(bid, annotator_id)
            if reviewer_set:
                batch.reviewer_id = reviewer_id
                await self._cascade_task_reviewer(bid, reviewer_id)
            self._sync_assigned_user_ids(batch)
            succeeded.append(bid)
        await self.db.flush()
        # issue #124 · 批量改派后广播, 让其它管理员的「可预标」列表实时收敛。
        if succeeded:
            await publish_batch_assignment_change(
                str(project_id), [str(b) for b in succeeded]
            )
        return {"succeeded": succeeded, "skipped": [], "failed": failed}

    async def bulk_activate(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
    ) -> dict[str, list[dict]]:
        """逐个 draft → active。前置不满足（无 annotator / 0 task）→ failed。"""
        loaded = await self._list_batches_in_project(project_id, batch_ids)
        succeeded: list[uuid.UUID] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if batch.status == BatchStatus.ACTIVE:
                skipped.append({"batch_id": bid, "reason": "already active"})
                continue
            if batch.status != BatchStatus.DRAFT:
                failed.append(
                    {
                        "batch_id": bid,
                        "reason": f"cannot activate from '{batch.status}'",
                    }
                )
                continue
            if batch.annotator_id is None:
                failed.append({"batch_id": bid, "reason": "no annotator assigned"})
                continue
            count = (
                await self.db.execute(
                    select(func.count()).select_from(Task).where(Task.batch_id == bid)
                )
            ).scalar() or 0
            if count == 0:
                failed.append({"batch_id": bid, "reason": "batch has no tasks"})
                continue
            batch.status = BatchStatus.ACTIVE
            succeeded.append(bid)
        await self.db.flush()
        return {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    # ── v0.9.15 · ADR-0008 Admin Lock ─────────────────────────────────────

    async def admin_lock(
        self,
        batch_id: uuid.UUID,
        *,
        reason: str,
        locked_by: uuid.UUID,
    ) -> TaskBatch:
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        if batch.admin_locked:
            raise HTTPException(status_code=409, detail="Batch is already locked")
        batch.admin_locked = True
        batch.admin_lock_reason = reason
        batch.admin_locked_at = datetime.now(timezone.utc)
        batch.admin_locked_by = locked_by
        await self.db.flush()
        return batch

    async def admin_unlock(self, batch_id: uuid.UUID) -> TaskBatch:
        batch = await self.db.get(TaskBatch, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        if not batch.admin_locked:
            raise HTTPException(status_code=409, detail="Batch is not locked")
        batch.admin_locked = False
        batch.admin_lock_reason = None
        batch.admin_locked_at = None
        batch.admin_locked_by = None
        await self.db.flush()
        return batch

    # ── v0.9.15 · Bulk Approve/Reject ──────────────────────────────────────

    async def bulk_approve(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
        *,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, list]:
        loaded = await self._lock_decision_scope(
            actor_id, batch_ids, project_id=project_id
        )
        succeeded: list[uuid.UUID] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if batch.status == BatchStatus.APPROVED:
                skipped.append({"batch_id": bid, "reason": "already approved"})
                continue
            if batch.admin_locked:
                failed.append({"batch_id": bid, "reason": "batch is admin-locked"})
                continue
            if batch.status != BatchStatus.REVIEWING:
                failed.append(
                    {"batch_id": bid, "reason": f"cannot approve from '{batch.status}'"}
                )
                continue
            try:
                await self._assert_locked_batch_evidence(batch, actor_id)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                reason = detail.get("reason")
                if reason not in {
                    "review_contributors_unknown",
                    "self_review_denied",
                }:
                    # Lock/capability failures are request-level and may have
                    # rolled the transaction back; do not continue a bulk.
                    raise
                failed.append({"batch_id": bid, "reason": reason})
                continue
            batch.status = BatchStatus.APPROVED
            succeeded.append(bid)
        await self.db.flush()
        return {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    async def bulk_reject(
        self,
        project_id: uuid.UUID,
        batch_ids: list[uuid.UUID],
        *,
        feedback: str,
        reviewer_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> dict[str, list]:
        loaded = await self._lock_decision_scope(
            actor_id or reviewer_id, batch_ids, project_id=project_id
        )
        succeeded: list[uuid.UUID] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for bid in batch_ids:
            batch = loaded.get(bid)
            if batch is None:
                failed.append({"batch_id": bid, "reason": "not found"})
                continue
            if batch.status == BatchStatus.REJECTED:
                skipped.append({"batch_id": bid, "reason": "already rejected"})
                continue
            if batch.status != BatchStatus.REVIEWING:
                failed.append(
                    {"batch_id": bid, "reason": f"cannot reject from '{batch.status}'"}
                )
                continue
            try:
                await self._assert_locked_batch_evidence(batch, actor_id or reviewer_id)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                reason = detail.get("reason")
                if reason not in {
                    "review_contributors_unknown",
                    "self_review_denied",
                }:
                    # Lock/capability failures are request-level and may have
                    # rolled the transaction back; do not continue a bulk.
                    raise
                failed.append({"batch_id": bid, "reason": reason})
                continue
            # soft reset: review/completed tasks → pending
            # The current round's frozen evidence is cleared with the reset,
            # matching single-batch rejection, so stale evidence from a
            # completed round is never mistaken for current review authority.
            await self.db.execute(
                update(Task)
                .where(
                    Task.batch_id == bid,
                    Task.status.in_(["review", "completed"]),
                )
                .values(
                    status="pending",
                    is_labeled=False,
                    review_contributor_ids=None,
                    review_submitter_id=None,
                )
            )
            batch.status = BatchStatus.REJECTED
            batch.review_feedback = feedback
            batch.reviewed_at = datetime.now(timezone.utc)
            batch.reviewed_by = reviewer_id
            succeeded.append(bid)
        await self.db.flush()
        for bid in succeeded:
            await self._recalculate_batch_counters(bid)
        return {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    async def _load_batches_for_bulk(
        self, project_id: uuid.UUID, batch_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, TaskBatch]:
        rows = await self.db.execute(
            select(TaskBatch).where(
                TaskBatch.id.in_(batch_ids),
                TaskBatch.project_id == project_id,
            )
        )
        return {b.id: b for b in rows.scalars().all()}

    async def _recalculate_batch_counters(self, batch_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(
                func.count(Task.id).label("total"),
                func.sum(func.cast(Task.is_labeled, Integer)).label("completed"),
                func.sum(func.cast(Task.status == "review", Integer)).label("review"),
            ).where(Task.batch_id == batch_id)
        )
        row = result.one()
        batch = await self.db.get(TaskBatch, batch_id)
        if batch:
            batch.total_tasks = row.total or 0
            batch.completed_tasks = row.completed or 0
            batch.review_tasks = row.review or 0
            await self.db.flush()

    # ── AI/ML hook ─────────────────────────────────────────────────────────

    async def on_batch_approved(self, batch_id: uuid.UUID) -> None:
        # TODO(v0.7.x+)：active learning 闭环 — 把已通过批次推回 ML backend 训练队列。
        # 依赖 ML backend / 训练队列基座（ROADMAP A · AI/模型 区）落地后再实现。
        logger.info(
            "on_batch_approved hook: batch_id=%s — no-op (reserved for active learning)",
            batch_id,
        )
