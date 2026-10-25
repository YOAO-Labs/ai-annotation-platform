from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes, make_transient_to_detached
from sqlalchemy.orm.util import identity_key

from aap_labeling.task_locks_contract import (
    TASK_LOCKS_FUNCTION,
    TaskLockRecord,
    TaskLocksUnavailable,
    task_locks_command,
)
from aap_labeling.task_locks_data import TaskLocksData
from app.db.models.task_lock import TaskLock
from app.services.identity_account_lock_data import IdentityAccountLockData

_RETRYABLE_SQLSTATES = {"40P01", "40001"}
_MAX_DEADLOCK_RETRY = 3
_STEP = text(f"SELECT {TASK_LOCKS_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))")


class TaskLockConflictError(RuntimeError):
    pass


async def assert_task_user_active(
    db: AsyncSession, user_id: uuid.UUID, *, lock: bool = True
) -> bool:
    """Serialize task acquisition with Identity account suspension."""

    if lock:
        accounts = await IdentityAccountLockData(db).lock_accounts(
            [user_id] if user_id is not None else [], mode="share", nowait=False
        )
        actor = accounts.get(user_id)
    else:
        actor = await IdentityAccountLockData(db).read_actor_status(user_id)
    return actor is not None and actor.is_active


def _is_retryable_db_error(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate in _RETRYABLE_SQLSTATES


class TaskLockService:
    DEFAULT_TTL = 300

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._data = TaskLocksData(self._call)

    async def _call(self, request):
        return await self.db.scalar(
            _STEP,
            {
                "request": json.dumps(
                    request, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                )
            },
        )

    async def _execute(self, action: str, **values):
        command = task_locks_command(action, **values)
        try:
            return await self._data.execute(command)
        except DBAPIError as exc:
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
            if sqlstate == "55000":
                raise HTTPException(
                    status_code=503, detail="身份认证服务暂不可用"
                ) from None
            raise
        except TaskLocksUnavailable:
            raise HTTPException(
                status_code=503, detail="任务编辑锁服务暂不可用"
            ) from None

    async def _flush_for_orm_query(self) -> None:
        if self.db.autoflush:
            await self.db.flush()

    def _expire_cached_ids(self, ids) -> None:
        for lock_id in ids:
            key = identity_key(TaskLock, ident=lock_id)
            instance = self.db.sync_session.identity_map.get(key)
            if instance is not None:
                self.db.expunge(instance)

    async def _attach(self, record: TaskLockRecord) -> TaskLock:
        key = identity_key(TaskLock, ident=record.id)
        instance = self.db.sync_session.identity_map.get(key)
        values = record.model_dump(mode="python")
        if instance is not None:
            state = attributes.instance_state(instance)
            for name, value in values.items():
                if name in state.expired_attributes:
                    attributes.set_committed_value(instance, name, value)
            return instance

        detached = TaskLock(**values)
        make_transient_to_detached(detached)
        return await self.db.merge(detached, load=False)

    async def assert_final_write_available(
        self, task_id: uuid.UUID, actor_id: uuid.UUID, *, allow_own: bool
    ) -> None:
        """Fence editor acquisition through the final write transaction."""
        result = await self._execute("try_final_scope", task_id=task_id)
        if not result["available"]:
            raise HTTPException(status_code=409, detail={"reason": "task_busy"})
        locks = await self.active_locks(task_id)
        if any(not allow_own or lock.user_id != actor_id for lock in locks):
            raise HTTPException(
                status_code=409, detail={"reason": "task_locked_for_editing"}
            )

    async def batch_final_write_available(
        self,
        batch_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        allow_own: bool,
        allowed_statuses: frozenset[str],
    ) -> bool:
        """Fence the finite batch task/editor set without per-task queries."""
        await self._flush_for_orm_query()
        try:
            result = await self._execute(
                "batch_final_write_available",
                batch_id=batch_id,
                actor_id=actor_id,
                allow_own=allow_own,
                allowed_statuses=tuple(sorted(allowed_statuses)),
            )
        except DBAPIError as exc:
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
            if sqlstate in {"55P03", "40001", "40P01"}:
                raise HTTPException(
                    status_code=409, detail={"reason": "task_busy"}
                ) from exc
            raise
        return result["available"]

    async def _lock_task_scope(self, task_id: uuid.UUID) -> None:
        """Serialize TaskLock operations with annotation writes in this transaction."""

        await self._execute("lock_scope", task_id=task_id)

    async def acquire(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        ttl: int | None = None,
        force_takeover: bool = False,
    ) -> TaskLock | None:
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
        return None

    async def _acquire_once(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        ttl: int | None = None,
        force_takeover: bool = False,
    ) -> TaskLock | None:
        # IdentityAccountLockData used to flush here before its account SHARE lock.
        await self._flush_for_orm_query()
        result = await self._execute(
            "acquire",
            task_id=task_id,
            user_id=user_id,
            ttl=ttl,
            force_takeover=force_takeover,
        )
        self._expire_cached_ids(result["deleted_ids"])
        self._expire_cached_ids(result["considered_ids"])
        record = result["lock"]
        if record is None:
            return None
        # Preserve the old explicit flush after the successful upsert. Cached rows
        # selected for this task were expunged before it can write over the new row.
        await self.db.flush()
        return await self._attach(record)

    async def release(self, task_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        await self._lock_task_scope(task_id)
        await self._flush_for_orm_query()
        result = await self._execute("release", task_id=task_id, user_id=user_id)
        if not result["released"]:
            return False
        self._expire_cached_ids([result["lock"].id])
        await self.db.flush()
        return True

    async def heartbeat(
        self, task_id: uuid.UUID, user_id: uuid.UUID, ttl: int | None = None
    ) -> bool:
        await self._lock_task_scope(task_id)
        await self._flush_for_orm_query()
        result = await self._execute(
            "heartbeat", task_id=task_id, user_id=user_id, ttl=ttl
        )
        if not result["renewed"]:
            return False
        row = result["lock"]
        cached = await self._attach(row)
        attributes.set_committed_value(cached, "expire_at", row.expire_at)
        await self.db.flush()
        return True

    async def assert_write_allowed(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Reject a write while another user owns the active task edit lock."""

        await self._lock_task_scope(task_id)
        await self._flush_for_orm_query()
        result = await self._execute(
            "assert_write_allowed", task_id=task_id, user_id=user_id
        )
        self._expire_cached_ids(result["deleted_ids"])
        for row in result["locks"]:
            await self._attach(row)
        if result["owner_id"] is not None:
            raise TaskLockConflictError("task is locked by another user")

    async def is_locked(self, task_id: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        result = await self._read("is_locked", task_id)
        if result["lock"] is not None:
            await self._attach(result["lock"])
        return result["locked"], result["user_id"]

    async def active_lock(self, task_id: uuid.UUID) -> TaskLock | None:
        result = await self._read("active_lock", task_id)
        record = result["lock"]
        return None if record is None else await self._attach(record)

    async def active_locks(self, task_id: uuid.UUID) -> list[TaskLock]:
        result = await self._read("active_locks", task_id)
        return [await self._attach(row) for row in result["locks"]]

    async def _read(self, action: str, task_id: uuid.UUID):
        await self._flush_for_orm_query()
        result = await self._execute(action, task_id=task_id)
        self._expire_cached_ids(result["deleted_ids"])
        return result

    async def _cleanup_expired(self, task_id: uuid.UUID | None = None) -> int:
        await self._flush_for_orm_query()
        result = await self._execute("cleanup_expired", task_id=task_id)
        self._expire_cached_ids(result["deleted_ids"])
        return result["deleted_count"]
