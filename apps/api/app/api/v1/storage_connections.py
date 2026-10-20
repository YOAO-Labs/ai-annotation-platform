"""v0.11.16 · 存储连接器 API（服务端拉取导入的连接配置 + 超管主机白名单）。

权限：
  - global-scope 连接器、主机白名单：仅 super_admin。
  - owner-scope 连接器：project_admin 可建，创建者或 super_admin 可改/删。
  - 查看/测试/导入：global、创建者本人或 super_admin 可用。
密钥永不回吐（Out 仅 secret_set:bool）。create/test/import 三处都过白名单 + SSRF。
"""

from __future__ import annotations

import uuid
from ipaddress import ip_address

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_audit_contract import ProjectDeletionAuditContext
from aap_assets_source.connections_contract import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ReadConnectionRequest,
    TestConnectionRequest,
    UpdateConnectionRequest,
)
from app.config import settings
from app.db.enums import UserRole
from app.deps import get_current_user, get_db, require_roles
from app.schemas.storage_connection import (
    ConnectorAllowlistOut,
    ConnectorAllowlistUpdate,
    ConnectorDeploymentSftpPresetOut,
    StorageConnectionCreate,
    StorageConnectionOut,
    StorageConnectionTestResult,
    StorageConnectionUpdate,
)
from app.services import connector_guard
from app.middleware.request_id import request_id_var
from app.services.audit import AuditAction, AuditService, audit_origin_from_request
from app.services.assets_connections import AssetsConnectionsGateway
from app.services.system_settings_service import SystemSettingsService
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


def _connection_audit_context(request: Request) -> ProjectDeletionAuditContext:
    origin = audit_origin_from_request(request)
    try:
        ip = str(ip_address(origin.ip)) if origin.ip else None
    except ValueError:
        ip = None
    return ProjectDeletionAuditContext(
        method=origin.method,
        path=origin.path,
        ip=ip,
        request_id=request_id_var.get()[:36] or None,
    )


def _connection_out(connection) -> StorageConnectionOut:
    return StorageConnectionOut.model_validate(connection.model_dump(mode="python"))


def _connection_request(model: type[BaseModel], **values):
    try:
        return model(**values)
    except (ValidationError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="连接器请求无效") from None


# ---- 主机白名单（仅 super_admin）。先于 /{conn_id} 注册避免被路径吞掉。----


@router.get("/allowlist", response_model=ConnectorAllowlistOut)
async def get_allowlist(
    db: AsyncSession = Depends(get_db),
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    entries, source = await connector_guard.get_allowlist_state(db)
    return ConnectorAllowlistOut(entries=entries, source=source)


@router.put("/allowlist", response_model=ConnectorAllowlistOut)
async def update_allowlist(
    payload: ConnectorAllowlistUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
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
    actor: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
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
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    host = settings.connector_deployment_sftp_host.strip()
    return ConnectorDeploymentSftpPresetOut(
        enabled=bool(host), host=host or None, port=22
    )


# ---- 连接器 CRUD ----


@router.get("", response_model=list[StorageConnectionOut])
async def list_connections(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    connections = await AssetsConnectionsGateway(db).list_connections(user.id)
    return [_connection_out(connection) for connection in connections]


@router.post("", response_model=StorageConnectionOut, status_code=201)
async def create_connection(
    payload: StorageConnectionCreate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    if payload.scope == "global":
        if user.role != UserRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="仅超级管理员可建全局连接器")

    operation_id = idempotency_key or uuid.uuid4()
    result = await AssetsConnectionsGateway(db).create_connection(
        _connection_request(
            CreateConnectionRequest,
            operation_id=operation_id,
            actor_id=user.id,
            name=payload.name,
            kind=payload.kind,
            config=payload.config,
            secret=payload.secret,
            scope=payload.scope,
            audit_context=_connection_audit_context(request),
        )
    )
    response.headers["Idempotency-Key"] = str(result.operation_id)
    assert result.connection is not None
    return _connection_out(result.connection)


@router.get("/{conn_id}", response_model=StorageConnectionOut)
async def get_connection(
    conn_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    result = await AssetsConnectionsGateway(db).read_connection(
        _connection_request(
            ReadConnectionRequest, actor_id=user.id, connection_id=conn_id
        )
    )
    return _connection_out(result.connection)


@router.patch("/{conn_id}", response_model=StorageConnectionOut)
async def update_connection(
    conn_id: uuid.UUID,
    payload: StorageConnectionUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key or uuid.uuid4()
    result = await AssetsConnectionsGateway(db).update_connection(
        _connection_request(
            UpdateConnectionRequest,
            operation_id=operation_id,
            actor_id=user.id,
            connection_id=conn_id,
            name=payload.name,
            config=payload.config,
            secret=payload.secret,
            audit_context=_connection_audit_context(request),
        )
    )
    response.headers["Idempotency-Key"] = str(result.operation_id)
    assert result.connection is not None
    return _connection_out(result.connection)


@router.delete("/{conn_id}", status_code=204)
async def delete_connection(
    conn_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key or uuid.uuid4()
    await AssetsConnectionsGateway(db).delete_connection(
        _connection_request(
            DeleteConnectionRequest,
            operation_id=operation_id,
            actor_id=user.id,
            connection_id=conn_id,
            audit_context=_connection_audit_context(request),
        )
    )
    response.headers["Idempotency-Key"] = str(operation_id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/{conn_id}/test", response_model=StorageConnectionTestResult)
async def test_connection(
    conn_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key or uuid.uuid4()
    result = await AssetsConnectionsGateway(db).test_connection(
        _connection_request(
            TestConnectionRequest,
            operation_id=operation_id,
            actor_id=user.id,
            connection_id=conn_id,
            audit_context=_connection_audit_context(request),
        )
    )
    response.headers["Idempotency-Key"] = str(result.operation_id)
    return StorageConnectionTestResult(
        ok=result.ok, message=result.message, sample_count=result.sample_count
    )
