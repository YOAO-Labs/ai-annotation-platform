import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
)
from app.db.models.user import User
from app.schemas.task import (
    TaskLockResponse,
)
from app.services.project_access import ProjectAccess
from app.services.task_lock import TaskLockService
from app.services.user_brief import resolve_briefs


from app.api.v1.tasks._shared import (
    _load_task_or_404,
    _assert_task_visible,
    _effective_task_assignee_id,
    require_task_annotation_write_lifecycle,
)

router = APIRouter()


@router.post("/{task_id}/lock", response_model=TaskLockResponse)
async def acquire_lock(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    # B-21：任务的当前 assignee 重进时强制接管残留锁，
    # 否则上一个会话残留的他人 lock 会让本人误判"他人正在编辑"。
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    is_assignee = (await _effective_task_assignee_id(db, task)) == current_user.id
    svc = TaskLockService(db)
    lock = await svc.acquire(task_id, current_user.id, force_takeover=is_assignee)
    if not lock:
        active_lock = await svc.active_lock(task_id)
        locked_by = None
        if active_lock:
            briefs = await resolve_briefs(db, [active_lock.user_id])
            brief = briefs.get(str(active_lock.user_id))
            locked_by = brief.model_dump(mode="json") if brief else None
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "task_locked_by_other",
                "message": "Task is locked by another user",
                "user_id": str(active_lock.user_id) if active_lock else None,
                "expire_at": active_lock.expire_at.isoformat() if active_lock else None,
                "locked_by": locked_by,
            },
        )
    await db.commit()
    return lock


@router.post("/{task_id}/lock/heartbeat")
async def heartbeat_lock(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    svc = TaskLockService(db)
    ok = await svc.heartbeat(task_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="No active lock found")
    await db.commit()
    return {"status": "renewed"}


@router.delete("/{task_id}/lock", status_code=204)
async def release_lock(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_lifecycle),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    svc = TaskLockService(db)
    await svc.release(task_id, current_user.id)
    await db.commit()


# ── Helpers ──────────────────────────────────────────────────────────────────
