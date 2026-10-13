"""v0.9.15 · Scheduler 覆盖：check_auto_transitions + get_next_task 批次过滤

Phase 1 门控：本文件必须在当前代码（无 admin_locked）上全绿，
Phase 2 才能引入 admin_locked 短路逻辑。

覆盖三类：
1. TestAutoTransitionActiveToAnnotating  — active/pre_annotated → annotating
2. TestAutoTransitionAnnotatingToReviewing — annotating → reviewing
3. TestAutoTransitionBoundary           — None/不存在 batch / 非过渡态
4. TestGetNextTaskBatchFiltering        — get_next_task 仅从 active/annotating 批次取任务
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.display_id import next_display_id
from tests.factory import create_membership, build_tool_bindings


# ── 共用 seed helper ─────────────────────────────────────────────────────────


async def _seed(
    db: AsyncSession,
    owner_id: uuid.UUID,
    annotator_id: uuid.UUID,
    *,
    batch_status: str = "active",
    n_tasks: int = 2,
    task_status: str = "pending",
    is_labeled: bool = False,
):
    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="scheduler test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(p)
    await db.flush()

    await create_membership(
        db, project_id=pid, user_id=annotator_id, role="annotator", assigned_by=owner_id
    )

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id=await next_display_id(db, "batches"),
        name="b1",
        status=batch_status,
        annotator_id=annotator_id,
        assigned_user_ids=[str(annotator_id)],
    )
    db.add(batch)
    await db.flush()

    tasks: list[Task] = []
    for i in range(n_tasks):
        t = Task(
            id=uuid.uuid4(),
            project_id=pid,
            batch_id=batch.id,
            display_id=f"T-{i}",
            file_name=f"f{i}.jpg",
            file_path=f"/tmp/f{i}.jpg",
            file_type="image",
            status=task_status,
            is_labeled=is_labeled,
        )
        db.add(t)
        tasks.append(t)
    await db.flush()
    return p, batch, tasks


# ── 1. active/pre_annotated → annotating ────────────────────────────────────


class TestAutoTransitionActiveToAnnotating:
    @pytest.mark.asyncio
    async def test_active_to_annotating_on_in_progress(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )
        tasks[0].status = "in_progress"
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_active_to_annotating_on_rejected_task(
        self, db_session, super_admin, annotator
    ):
        """M1：rejected 任务也算"标注进行中"，应触发 active→annotating。"""
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )
        tasks[0].status = "rejected"
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_active_stays_when_all_pending(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "active"

    @pytest.mark.asyncio
    async def test_pre_annotated_to_annotating_on_in_progress(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="pre_annotated",
            task_status="pending",
        )
        tasks[0].status = "in_progress"
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_pre_annotated_stays_when_all_pending(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="pre_annotated",
            task_status="pending",
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "pre_annotated"


# ── 2. annotating → reviewing ────────────────────────────────────────────────


class TestAutoTransitionAnnotatingToReviewing:
    @pytest.mark.asyncio
    async def test_annotating_to_reviewing_when_all_review(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="review",
            is_labeled=True,
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "reviewing"

    @pytest.mark.asyncio
    async def test_annotating_to_reviewing_when_all_completed(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="completed",
            is_labeled=True,
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "reviewing"

    @pytest.mark.asyncio
    async def test_annotating_stays_when_pending_exists(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="review",
            is_labeled=True,
        )
        tasks[0].status = "pending"
        tasks[0].is_labeled = False
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_annotating_stays_when_in_progress_exists(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="review",
            is_labeled=True,
        )
        tasks[0].status = "in_progress"
        tasks[0].is_labeled = False
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_annotating_stays_when_rejected_exists(
        self, db_session, super_admin, annotator
    ):
        """M1：rejected 任务算"未完成"，不能推 reviewing。"""
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="review",
            is_labeled=True,
        )
        tasks[0].status = "rejected"
        tasks[0].is_labeled = False
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "annotating"


# ── 3. 边界情况 ──────────────────────────────────────────────────────────────


class TestAutoTransitionBoundary:
    @pytest.mark.asyncio
    async def test_none_batch_id_is_noop(self, db_session, super_admin, annotator):
        from app.services.batch import BatchService

        await BatchService(db_session).check_auto_transitions(None)
        # 没有抛出异常即通过

    @pytest.mark.asyncio
    async def test_nonexistent_batch_id_is_noop(
        self, db_session, super_admin, annotator
    ):
        from app.services.batch import BatchService

        await BatchService(db_session).check_auto_transitions(uuid.uuid4())
        # 没有抛出异常即通过

    @pytest.mark.asyncio
    async def test_reviewing_status_is_noop(self, db_session, super_admin, annotator):
        """reviewing 态不在 check_auto_transitions 处理范围，不应变更。"""
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
            task_status="review",
            is_labeled=True,
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "reviewing"

    @pytest.mark.asyncio
    async def test_approved_status_is_noop(self, db_session, super_admin, annotator):
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="approved",
            task_status="completed",
            is_labeled=True,
        )

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "approved"


# ── 4. get_next_task 批次状态过滤 ────────────────────────────────────────────


class TestGetNextTaskBatchFiltering:
    @pytest.mark.asyncio
    async def test_picks_task_from_active_batch(
        self, db_session, super_admin, annotator
    ):
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is not None
        assert result.batch_id == batch.id

    @pytest.mark.asyncio
    async def test_picks_task_from_annotating_batch(
        self, db_session, super_admin, annotator
    ):
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="pending",
        )
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is not None
        assert result.batch_id == batch.id

    @pytest.mark.asyncio
    async def test_ignores_draft_batch(self, db_session, super_admin, annotator):
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="draft",
            task_status="pending",
        )
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_reviewing_batch(self, db_session, super_admin, annotator):
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
            task_status="review",
            is_labeled=True,
        )
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_archived_batch(self, db_session, super_admin, annotator):
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="archived",
            task_status="pending",
        )
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is None
