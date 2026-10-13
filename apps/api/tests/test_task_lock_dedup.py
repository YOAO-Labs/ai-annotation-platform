"""B-6 回归：task_locks 表 unique 约束是 (task_id, user_id)，不阻止同 task_id 多行。

历史并发 / 残留可能让同一 task_id 出现多行（不同 user_id）。
旧实现里 TaskLockService.acquire / is_locked 与 scheduler.get_next_task
都用 scalar_one_or_none()，遇到多行直接抛 MultipleResultsFound → 500，
前端会同时弹"服务器错误"toast 和"该任务正被其他用户编辑"横幅（即 BUG B-6）。

本套校验：多行残留下，acquire / is_locked 不再 500，并按"我的锁优先 + 清理重复"的策略归一。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.services.task_lock import TaskLockService
from tests.factory import create_membership, build_tool_bindings


async def _seed_project_and_task(db: AsyncSession, owner_id: uuid.UUID) -> Task:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-LD-{suffix}",
        name="LockDedup",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(project)
    await db.flush()

    # The owner identity is a literal employee; its annotation authority comes
    # from this explicit membership, not from the account role.
    await create_membership(
        db,
        project_id=project.id,
        user_id=owner_id,
        role="annotator",
        assigned_by=owner_id,
    )

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-LD-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="in_progress",
        assignee_id=owner_id,
    )
    db.add(task)
    await db.flush()
    return task


def _stale_lock(task_id: uuid.UUID, user_id: uuid.UUID, ttl_s: int = 300) -> TaskLock:
    return TaskLock(
        id=uuid.uuid4(),
        task_id=task_id,
        user_id=user_id,
        expire_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_s),
        unique_id=uuid.uuid4(),
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTaskLockMultiRowResilience:
    async def test_acquire_with_my_existing_and_others_stale_renews_and_dedups(
        self, db_session, annotator, reviewer
    ):
        """同 task 出现 [我, 他] 两行残留 → acquire 应续期我的锁，并删除他人残留。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        db_session.add(_stale_lock(task.id, ann_user.id))
        db_session.add(_stale_lock(task.id, rev_user.id))
        await db_session.flush()

        svc = TaskLockService(db_session)
        lock = await svc.acquire(task.id, ann_user.id)

        assert lock is not None
        assert lock.user_id == ann_user.id

        rows = (
            (
                await db_session.execute(
                    select(TaskLock).where(TaskLock.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == ann_user.id

    async def test_acquire_when_only_others_hold_returns_none(
        self, db_session, annotator, reviewer
    ):
        """只剩他人锁残留时，我 acquire 应返回 None（不抛 500），由 endpoint 上抛 409。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        db_session.add(_stale_lock(task.id, rev_user.id))
        await db_session.flush()

        svc = TaskLockService(db_session)
        lock = await svc.acquire(task.id, ann_user.id)
        assert lock is None

    async def test_acquire_takes_over_when_other_lock_is_near_expiry(
        self, db_session, annotator, reviewer
    ):
        """v0.6.7 B-13：他人锁 expire_at < now + TTL/2（即 last heartbeat > 150s 前）→ 视为悬挂残留自动接管。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        # 他人残留锁：expire_at = now + 60s（远小于阈值 now + 150s）
        db_session.add(_stale_lock(task.id, rev_user.id, ttl_s=60))
        await db_session.flush()

        svc = TaskLockService(db_session)
        lock = await svc.acquire(task.id, ann_user.id)

        assert lock is not None, "悬挂残留锁应被自动接管"
        assert lock.user_id == ann_user.id

        rows = (
            (
                await db_session.execute(
                    select(TaskLock).where(TaskLock.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == ann_user.id

    async def test_acquire_blocks_when_other_lock_is_freshly_heartbeated(
        self, db_session, annotator, reviewer
    ):
        """v0.6.7 B-13 反向用例：他人锁 expire_at > now + TTL/2（活会话）→ 仍 409，不能接管。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        # 他人活锁：expire_at = now + 280s（活会话 60s 前刚心跳过）
        db_session.add(_stale_lock(task.id, rev_user.id, ttl_s=280))
        await db_session.flush()

        svc = TaskLockService(db_session)
        lock = await svc.acquire(task.id, ann_user.id)
        assert lock is None, "他人活锁未到 stale 阈值，不能强占"

    async def test_acquire_idempotent_under_concurrency_same_user(
        self, db_session, annotator
    ):
        """v0.6.7 二修 B-13：同用户对同 task 并发 acquire 不应撞 unique 约束抛 500。

        旧实现两个 tx 都看到 empty → 都 INSERT → 第二个撞 unique(task_id, user_id)。
        upsert 后第二次走 ON CONFLICT DO UPDATE。
        """
        ann_user, _ = annotator
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        svc = TaskLockService(db_session)
        lock1 = await svc.acquire(task.id, ann_user.id)
        lock2 = await svc.acquire(task.id, ann_user.id)

        # 两次都应成功，且只有一行（同 (task_id, user_id) 行 expire_at 续期）
        assert lock1 is not None
        assert lock2 is not None
        rows = (
            (
                await db_session.execute(
                    select(TaskLock).where(TaskLock.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == ann_user.id

    async def test_acquire_force_takeover_clears_others_for_assignee(
        self, db_session, annotator, reviewer
    ):
        """B-21：assignee 重进时 force_takeover=True 应直接清掉他人锁。

        旧逻辑要等他人锁 expire_at < now + TTL/2 才接管，导致用户退出后短时间重进
        被自己 4 分钟前的旁路锁挡住，看到"他人正在编辑"提示。
        """
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        # 他人活锁：expire_at = now + 280s（按现有阈值不会被自动接管）
        db_session.add(_stale_lock(task.id, rev_user.id, ttl_s=280))
        await db_session.flush()

        svc = TaskLockService(db_session)
        lock = await svc.acquire(task.id, ann_user.id, force_takeover=True)
        assert lock is not None
        assert lock.user_id == ann_user.id

        rows = (
            (
                await db_session.execute(
                    select(TaskLock).where(TaskLock.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == ann_user.id

    async def test_is_locked_tolerates_multi_row(self, db_session, annotator, reviewer):
        """is_locked 在多行残留下也不能 500。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)

        db_session.add(_stale_lock(task.id, ann_user.id))
        db_session.add(_stale_lock(task.id, rev_user.id))
        await db_session.flush()

        svc = TaskLockService(db_session)
        locked, _holder = await svc.is_locked(task.id)
        assert locked is True

    async def test_acquire_conflict_returns_lock_holder_detail(
        self, httpx_client, db_session, annotator, reviewer, super_admin
    ):
        """活跃他人锁冲突时，API 返回可展示的锁持有人信息。"""
        ann_user, _ = annotator
        rev_user, _ = reviewer
        _, admin_token = super_admin
        task = await _seed_project_and_task(db_session, owner_id=ann_user.id)
        # The legitimate manager can see the task, while the task belongs to the
        # other user; the caller is therefore not the effective assignee, so the
        # real lock-conflict path runs instead of the assignee force-takeover.
        task.assignee_id = rev_user.id
        db_session.add(_stale_lock(task.id, rev_user.id, ttl_s=280))
        await db_session.flush()

        response = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/lock",
            headers=_bearer(admin_token),
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["reason"] == "task_locked_by_other"
        assert detail["user_id"] == str(rev_user.id)
        assert detail["expire_at"]
        assert detail["locked_by"]["id"] == str(rev_user.id)
        assert detail["locked_by"]["name"] == rev_user.name
        assert detail["locked_by"]["email"] == rev_user.email
