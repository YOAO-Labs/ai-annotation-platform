"""v0.11.16 · 存储连接器 API（服务端拉取导入的连接配置 + 超管主机白名单）。

权限：
  - global-scope 连接器、主机白名单：仅 super_admin。
  - owner-scope 连接器：project_admin 可建，创建者或 super_admin 可改/删。
  - 配置查看：仅 project_admin / super_admin，API Key 需 datasets:read。
  - 测试/导入：沿各入口角色及连接使用范围校验。
密钥永不回吐（Out 仅 secret_set:bool）。create/test/import 三处都过白名单 + SSRF。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.crypto import ConnectorCryptoNotConfigured
from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_roles, assert_request_scopes
from app.schemas.storage_connection import (
    ConnectorAllowlistOut,
    ConnectorAllowlistUpdate,
    ConnectorDeploymentSftpPresetOut,
    StorageConnectionCreate,
    StorageConnectionOut,
    StorageConnectionTestResult,
    StorageConnectionUpdate,
)
from app.services.content_snapshot import ContentError
from app.services import connector_guard
from app.services.audit import AuditAction, AuditService
from app.services.storage_connection import (
    ConnectorAccessDenied,
    ConnectorValidationError,
    StorageConnectionService,
    assert_connection_admin,
    assert_connection_usable,
    to_out_dict,
)
from app.services.system_settings_service import SystemSettingsService

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


def _crypto_guard(exc: ConnectorCryptoNotConfigured) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
    )


def _denied(exc: connector_guard.ConnectorHostDenied) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ---- 主机白名单（仅 super_admin）。先于 /{conn_id} 注册避免被路径吞掉。----


@router.get("/allowlist", response_model=ConnectorAllowlistOut)
async def get_allowlist(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    entries, source = await connector_guard.get_allowlist_state(db)
    return ConnectorAllowlistOut(entries=entries, source=source)


@router.put("/allowlist", response_model=ConnectorAllowlistOut)
async def update_allowlist(
    payload: ConnectorAllowlistUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    try:
        entries = connector_guard.normalize_allowlist(payload.entries)
    except connector_guard.ConnectorAllowlistInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await SystemSettingsService.set_many(
        db, {"connector_host_allowlist": entries}, actor.id
    )
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.CONNECTOR_ALLOWLIST_UPDATE,
        target_type="connector",
        target_id="allowlist",
        request=request,
        status_code=200,
        detail={"mode": "override", "count": len(entries)},
    )
    await db.commit()
    return ConnectorAllowlistOut(entries=entries, source="database")


@router.delete("/allowlist", response_model=ConnectorAllowlistOut)
async def reset_allowlist(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    await SystemSettingsService.reset(db, "connector_host_allowlist")
    entries = list(settings.connector_host_allowlist)
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.CONNECTOR_ALLOWLIST_UPDATE,
        target_type="connector",
        target_id="allowlist",
        request=request,
        status_code=200,
        detail={"mode": "reset", "count": len(entries)},
    )
    await db.commit()
    return ConnectorAllowlistOut(entries=entries, source="environment")


@router.get("/deployment-sftp-preset", response_model=ConnectorDeploymentSftpPresetOut)
async def get_deployment_sftp_preset(
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    host = settings.connector_deployment_sftp_host.strip()
    return ConnectorDeploymentSftpPresetOut(
        enabled=bool(host), host=host or None, port=22
    )


# ---- 连接器 CRUD ----


@router.get(
    "",
    response_model=list[StorageConnectionOut],
    description="仅项目管理员或超级管理员可读配置；API Key 需 datasets:read。",
)
async def list_connections(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGERS)),
):
    assert_request_scopes(request, "datasets:read")
    is_super = user.role == UserRole.SUPER_ADMIN
    conns = await StorageConnectionService.list_visible(
        db, all_scopes=is_super, user_id=user.id
    )
    return [to_out_dict(c) for c in conns]


@router.post("", response_model=StorageConnectionOut, status_code=201)
async def create_connection(
    payload: StorageConnectionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGERS)),
):
    if payload.scope == "global":
        if user.role != UserRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="仅超级管理员可建全局连接器")

    try:
        conn = await StorageConnectionService.create(
            db,
            name=payload.name,
            kind=payload.kind,
            config=payload.config,
            secret=payload.secret,
            scope=payload.scope,
            created_by=user.id,
        )
    except ConnectorValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except connector_guard.ConnectorHostDenied as e:
        raise _denied(e)
    except ConnectorCryptoNotConfigured as e:
        raise _crypto_guard(e)

    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.STORAGE_CONNECTION_CREATE,
        target_type="storage_connection",
        target_id=str(conn.id),
        request=request,
        status_code=201,
        detail={"kind": conn.kind, "scope": conn.scope},
    )
    await db.commit()
    await db.refresh(conn)
    return to_out_dict(conn)


@router.get(
    "/{conn_id}",
    response_model=StorageConnectionOut,
    description="仅项目管理员或超级管理员可读可见连接配置；API Key 需 datasets:read。",
)
async def get_connection(
    conn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGERS)),
):
    assert_request_scopes(request, "datasets:read")
    conn = await StorageConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        assert_connection_usable(user, conn)
    except ConnectorAccessDenied:
        raise HTTPException(status_code=404, detail="连接器不存在")
    return to_out_dict(conn)


@router.patch("/{conn_id}", response_model=StorageConnectionOut)
async def update_connection(
    conn_id: uuid.UUID,
    payload: StorageConnectionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_request_scopes(request, "datasets:write")
    conn = await StorageConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        assert_connection_admin(user, conn)
    except ConnectorAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        conn = await StorageConnectionService.update(
            db,
            conn,
            name=payload.name,
            config=payload.config,
            secret=payload.secret,
            expected_revision=payload.expected_revision,
            actor_id=user.id,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except ConnectorAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except ConnectorValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except connector_guard.ConnectorHostDenied as e:
        raise _denied(e)
    except ConnectorCryptoNotConfigured as e:
        raise _crypto_guard(e)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.STORAGE_CONNECTION_UPDATE,
        target_type="storage_connection",
        target_id=str(conn.id),
        request=request,
        status_code=200,
        detail={
            "secret_rotated": payload.secret is not None,
            "revision": conn.revision,
        },
    )
    await db.commit()
    await db.refresh(conn)
    return to_out_dict(conn)


@router.delete("/{conn_id}", status_code=204)
async def delete_connection(
    conn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conn = await StorageConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        assert_connection_admin(user, conn)
    except ConnectorAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    await StorageConnectionService.delete(db, conn)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.STORAGE_CONNECTION_DELETE,
        target_type="storage_connection",
        target_id=str(conn_id),
        request=request,
        status_code=204,
    )
    await db.commit()


@router.post("/{conn_id}/test", response_model=StorageConnectionTestResult)
async def test_connection(
    conn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_request_scopes(request, "datasets:read")
    conn = await StorageConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        assert_connection_usable(user, conn)
    except ConnectorAccessDenied:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        ok, message, count = await StorageConnectionService.test_connection(
            db,
            conn,
            actor_id=user.id,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except ConnectorAccessDenied:
        raise HTTPException(status_code=404, detail="连接器不存在") from None
    except ConnectorValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except connector_guard.ConnectorHostDenied as e:
        raise _denied(e)
    except ConnectorCryptoNotConfigured as e:
        raise _crypto_guard(e)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.STORAGE_CONNECTION_TEST,
        target_type="storage_connection",
        target_id=str(conn_id),
        request=request,
        status_code=200,
        detail={"ok": ok},
    )
    await db.commit()
    return StorageConnectionTestResult(ok=ok, message=message, sample_count=count)
