"""v0.6.9 · 通知中心 REST。

- GET /notifications        list（unread_only / limit / offset）
- GET /notifications/unread-count  TopBar 角标
- POST /notifications/{id}/read    标记单条
- POST /notifications/mark-all-read 批量
- DELETE /notifications/{id}       删除单条
- POST /notifications/clear-read   删除所有已读
- GET/PUT /notification-preferences 按类型接收(in_app)与弹出(toast)偏好

读/删除/偏好变更的 ``notifications.sync`` 事件都在 ``db.commit()`` 成功后发布，
收到事件的会话总是读到已提交状态。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from aap_notifications.preferences import (
    KNOWN_NOTIFICATION_TYPES,
    NotificationPreferenceManager,
)
from aap_notifications.preferences_postgres import NotificationPreferencesPostgresData
from app.deps import get_db, get_current_user
from app.schemas.notification import NotificationList, NotificationOut, UnreadCount
from app.services.notification import NotificationService
from app.services.identity_contracts import IdentityActorFacts


class NotificationPreferenceItem(BaseModel):
    type: str
    in_app: bool = True
    email: bool = False
    # Effective popup choice: stored override, else the type's default.
    toast: bool = False


class NotificationPreferencesOut(BaseModel):
    items: list[NotificationPreferenceItem]


class NotificationPreferenceUpdate(BaseModel):
    type: str
    in_app: bool | None = None
    toast: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_channels(cls, data):
        if isinstance(data, dict):
            for key in ("in_app", "toast"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must be a boolean, not null")
        return data


router = APIRouter()


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    items, total, unread = await svc.list_for_user(
        user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationList(
        items=[NotificationOut.model_validate(i) for i in items],
        total=total,
        unread=unread,
    )


@router.get("/notifications/unread-count", response_model=UnreadCount)
async def unread_count(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    return UnreadCount(unread=await svc.unread_count(user.id))


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    ok = await svc.mark_read(user.id, notification_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail="Notification not found or already read"
        )
    await db.commit()
    # Commit-ordered sync: another session receiving this event reads committed state.
    await svc.publish_sync(user.id, reason="read")
    return {"ok": True}


@router.post("/notifications/mark-all-read")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    n = await svc.mark_all_read(user.id)
    await db.commit()
    if n > 0:
        await svc.publish_sync(user.id, reason="read")
    return {"updated": n}


@router.post("/notifications/clear-read")
async def clear_read_notifications(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    n = await svc.clear_read(user.id)
    await db.commit()
    if n > 0:
        await svc.publish_sync(user.id, reason="deleted")
    return {"deleted": n}


@router.delete("/notifications/{notification_id}")
async def delete_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    svc = NotificationService(db)
    ok = await svc.delete_for_user(user.id, notification_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
    await svc.publish_sync(user.id, reason="deleted")
    return {"ok": True}


@router.get("/notification-preferences", response_model=NotificationPreferencesOut)
async def get_preferences(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.7.0：返回所有已知 type 的偏好；无记录默认 in_app=True / email=False。

    toast 的未存储值由 Notifications 偏好合同按类型给出默认重要程度。
    """
    items = await NotificationPreferenceManager(
        NotificationPreferencesPostgresData(db)
    ).get(user.id)
    return NotificationPreferencesOut(
        items=[NotificationPreferenceItem.model_validate(item) for item in items]
    )


@router.put("/notification-preferences")
async def update_preference(
    data: NotificationPreferenceUpdate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    """Upsert 单条偏好；v0.7.0 起 in_app/toast 至少提供一个。

    只合并请求中出现的键（JSONB ``channels || patch``），保留未提供的
    in_app/toast/email 及其它字段；并发标签页写不同键互不覆盖。
    channels.email 字段保留但当前不消费。提交成功后发布
    ``notifications.sync reason=preferences``，让其它会话刷新偏好而不产生通知。
    """
    if data.type not in KNOWN_NOTIFICATION_TYPES:
        raise HTTPException(
            status_code=400, detail=f"unknown notification type: {data.type}"
        )
    if data.in_app is None and data.toast is None:
        raise HTTPException(
            status_code=400, detail="at least one of in_app or toast is required"
        )

    patch: dict[str, bool] = {}
    if data.in_app is not None:
        patch["in_app"] = data.in_app
    if data.toast is not None:
        patch["toast"] = data.toast
    await NotificationPreferenceManager(NotificationPreferencesPostgresData(db)).patch(
        user.id, data.type, patch
    )
    await db.commit()
    await NotificationService(db).publish_sync(user.id, reason="preferences")
    return {"ok": True}
