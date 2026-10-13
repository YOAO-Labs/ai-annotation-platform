from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task_lock import TaskLock
from app.db.models.user import User

# 退出重进时 acquire 与 release(DELETE)/并发 acquire 会在 task_locks 上交错加锁，
# Postgres 可能判定死锁(40P01) / 序列化失败(40001)。死锁会 abort 整个事务，
# 无法用 savepoint 局部恢复，只能 rollback 后整体重试。
_RETRYABLE_SQLSTATES = {"40P01", "40001"}
_MAX_DEADLOCK_RETRY = 3


class TaskLockConflictError(RuntimeError):
    pass


async def assert_task_user_active(
    db: AsyncSession, user_id: uuid.UUID, *, lock: bool = True
) -> bool:
    """Serialize task acquisition with account suspension.

    The shared read lock is acquired before the per-task advisory lock. User
    offboarding takes the matching User write lock before locking task rows, so
    a request that passed authentication before suspension either commits its
    task operation first or observes the inactive account and stops.
    """

    stmt = select(User.is_active).where(User.id == user_id)
    if lock:
        stmt = stmt.with_for_update(read=True)
    active = (await db.execute(stmt)).scalar_one_or_none()
    return bool(active)


def _is_retryable_db_error(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate in _RETRYABLE_SQLSTATES


class TaskLockService:
    DEFAULT_TTL = 300

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _lock_task_scope(self, task_id: uuid.UUID) -> None:
        """Serialize lock-table decisions with annotation writes for one task."""

        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"aap:task-edit-lock:{task_id}"},
        )

    async def acquire(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        ttl: int | None = None,
        force_takeover: bool = False,
    ) -> TaskLock | None:
        # 死锁兜底重试：两个调用点(acquire_lock 端点 / scheduler 取下一任务)在 acquire
        # 之前都只读不写，acquire 是事务内第一个写动作，故死锁后 rollback 整体重试是安全的。
        for attempt in range(_MAX_DEADLOCK_RETRY):
            try:
                return await self._acquire_once(
                    task_id, user_id, ttl=ttl, force_takeover=force_takeover
                )
            except DBAPIError as exc:
                if attempt + 1 < _MAX_DEADLOCK_RETRY and _is_retryable_db_error(exc):
                    await self.db.rollback()
                    continue
                raise
        return None  # unreachable: 循环要么 return 要么 raise

    async def _acquire_once(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        ttl: int | None = None,
        force_takeover: bool = False,
    ) -> TaskLock | None:
        if not await assert_task_user_active(self.db, user_id):
            return None
        await self._lock_task_scope(task_id)
        # B-6 修复：表上 unique 约束是 (task_id, user_id)，并不阻止同一 task_id 出现多行（不同用户）。
        # 历史并发 / 残留可能留下重复行，原本 scalar_one_or_none() 会抛 MultipleResultsFound → 500。
        # 这里改为读取全部行：若我已持有则续期并清掉同 task 的他人重复锁；否则视为他人占用。
        await self._cleanup_expired(task_id)

        result = await self.db.execute(
            select(TaskLock).where(TaskLock.task_id == task_id)
        )
        locks = list(result.scalars().all())

        # unique(task_id, user_id) 保证同一 (task, user) 至多一行，故 mine 至多 1 个。
        mine = next((lock for lock in locks if lock.user_id == user_id), None)
        others = [lock for lock in locks if lock.user_id != user_id]
        new_expire = datetime.now(timezone.utc) + timedelta(
            seconds=ttl or self.DEFAULT_TTL
        )

        # 关键修复（StaleDataError）：决策只用内存中的 locks 完成，随后把这些 ORM 行
        # 从 session 中 expunge。否则一旦后续 flush 时 ORM 对某个已被并发请求
        # (release DELETE / 另一个 acquire / _cleanup_expired) 删除的行发起 UPDATE/DELETE，
        # 受影响行数为 0，SQLAlchemy 抛 StaleDataError → 500。退出后立刻重进时高频触发。
        # 清理与续期一律改走 Core 语句 + upsert，不依赖这些 ORM 行仍然存在。
        for lock in locks:
            self.db.expunge(lock)

        if others and mine is None:
            # v0.6.7 B-13：他人锁存在但若全部「即将过期」（last heartbeat > TTL/2 前）→ 视为悬挂残留自动接管。
            # 真活会话每 60s 心跳一次，expire_at - now ∈ [240, 300]；阈值 TTL/2 = 150s 给两次心跳容错窗。
            #
            # v0.9.13 B-21：调用方判定 user 即任务 assignee 时显式 force_takeover —
            # 处理"标注员退出后他人残留锁 / 任务重派" 场景，避免本人重进被旧锁挡住。
            # reviewer 走非 assignee 路径仍按 stale_threshold 判定，不会被本机制破坏。
            now = datetime.now(timezone.utc)
            stale_threshold = now + timedelta(seconds=self.DEFAULT_TTL // 2)
            takeover = force_takeover or all(
                lock.expire_at < stale_threshold for lock in others
            )
            if not takeover:
                return None

        if others:
            # 走到此处：要么我已持锁（本人优先，无条件清理他人残影），要么已判定可接管。
            # 用 Core DELETE 而非 db.delete(orm_obj)，规避对并发已删行的 ORM DELETE flush。
            await self.db.execute(
                delete(TaskLock).where(
                    TaskLock.task_id == task_id, TaskLock.user_id != user_id
                )
            )

        # v0.6.7 二修 B-13：用 INSERT ... ON CONFLICT 而非裸 INSERT，避免快速重进时
        # 两个并发请求都看到「empty + my_lock=None」→ 都尝试 INSERT → 第二个撞
        # unique(task_id, user_id) 抛 IntegrityError → 500 → 前端误显「他人占用」横幅。
        # 既有行走 DO UPDATE 续期、不存在则插入，全程原子且不依赖 ORM 行。
        stmt = (
            pg_insert(TaskLock)
            .values(
                id=uuid.uuid4(),
                task_id=task_id,
                user_id=user_id,
                expire_at=new_expire,
                unique_id=uuid.uuid4(),
            )
            .on_conflict_do_update(
                index_elements=["task_id", "user_id"],
                set_={"expire_at": new_expire},
            )
            .returning(TaskLock.id)
        )
        result = await self.db.execute(stmt)
        lock_id = result.scalar_one()
        await self.db.flush()
        # 重新读出实际行（id 可能是新建的或既有的）
        return await self.db.get(TaskLock, lock_id)

    async def release(self, task_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        await self._lock_task_scope(task_id)
        result = await self.db.execute(
            select(TaskLock).where(
                TaskLock.task_id == task_id, TaskLock.user_id == user_id
            )
        )
        lock = result.scalar_one_or_none()
        if not lock:
            return False
        await self.db.delete(lock)
        await self.db.flush()
        return True

    async def heartbeat(
        self, task_id: uuid.UUID, user_id: uuid.UUID, ttl: int | None = None
    ) -> bool:
        await self._lock_task_scope(task_id)
        result = await self.db.execute(
            select(TaskLock).where(
                TaskLock.task_id == task_id, TaskLock.user_id == user_id
            )
        )
        lock = result.scalar_one_or_none()
        if not lock:
            return False
        lock.expire_at = datetime.now(timezone.utc) + timedelta(
            seconds=ttl or self.DEFAULT_TTL
        )
        await self.db.flush()
        return True

    async def assert_write_allowed(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Reject a write while another user owns the active task edit lock."""

        await self._lock_task_scope(task_id)
        await self._cleanup_expired(task_id)
        rows = await self.db.execute(
            select(TaskLock)
            .where(TaskLock.task_id == task_id)
            .order_by(TaskLock.expire_at.desc())
            .with_for_update()
        )
        owner = next(
            (lock.user_id for lock in rows.scalars() if lock.user_id != user_id),
            None,
        )
        if owner is not None:
            raise TaskLockConflictError("task is locked by another user")

    async def is_locked(self, task_id: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        # B-6 修复：见 acquire() 注释 — 同 task_id 可能有多行残留，使用 first() 兜底。
        await self._cleanup_expired(task_id)
        result = await self.db.execute(
            select(TaskLock).where(TaskLock.task_id == task_id)
        )
        lock = result.scalars().first()
        if lock:
            return True, lock.user_id
        return False, None

    async def active_lock(self, task_id: uuid.UUID) -> TaskLock | None:
        await self._cleanup_expired(task_id)
        result = await self.db.execute(
            select(TaskLock)
            .where(TaskLock.task_id == task_id)
            .order_by(TaskLock.expire_at.desc())
        )
        return result.scalars().first()

    async def active_locks(self, task_id: uuid.UUID) -> list[TaskLock]:
        """同一 task 的全部未过期锁。

        unique 约束是 ``(task_id, user_id)``, 并不阻止历史/并发残留产生多行
        (见 ``acquire`` 的清理说明), 因此「是否有他人持锁」必须逐行判断, 不能只看
        ``active_lock`` 返回的最新一行 (issue #121)。
        """
        await self._cleanup_expired(task_id)
        result = await self.db.execute(
            select(TaskLock).where(TaskLock.task_id == task_id)
        )
        return list(result.scalars().all())

    async def _cleanup_expired(self, task_id: uuid.UUID | None = None) -> int:
        # 收窄到单个 task_id：全表 DELETE 会跨任务锁住大量行，与并发 acquire 的
        # INSERT/UPDATE 交错时是死锁主因。按 task 清理后，不同任务的 acquire 互不争用，
        # 同任务的并发请求也只在本任务的少量行上排队。其他任务的过期行在其被访问时顺带清掉。
        now = datetime.now(timezone.utc)
        stmt = delete(TaskLock).where(TaskLock.expire_at < now)
        if task_id is not None:
            stmt = stmt.where(TaskLock.task_id == task_id)
        result = await self.db.execute(stmt)
        return result.rowcount
