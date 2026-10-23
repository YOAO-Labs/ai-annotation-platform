"""HTTP composition for the principal-bound Identity directory owner."""

from __future__ import annotations

import uuid

from aap_identity.contract import Principal
from aap_identity.directory_contract import DirectoryFailure, decode_directory_request
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_principal, get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_directory_commands import (
    DirectoryCommandsPort,
    IdentityDirectoryCommands,
)
from app.services.identity_gateway import IdentityGateway, get_identity_gateway


def require_directory_manager(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role not in {"super_admin", "project_admin"}:
        raise HTTPException(
            status_code=403, detail="需要角色权限: super_admin或project_admin"
        )
    return principal


def get_directory_commands(
    principal: Principal = Depends(require_directory_manager),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> DirectoryCommandsPort:
    return IdentityDirectoryCommands(db, identity, expected_principal=principal)


def _reject_directory_error(error: DirectoryFailure):
    if error.code in {
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
        "user_inactive",
        "invalid_account_role",
    }:
        raise HTTPException(
            status_code=401,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if error.code == "credential_busy":
        raise HTTPException(
            status_code=409, detail="账号正在更新，请刷新后重试"
        ) from None
    if error.code == "operation_conflict":
        raise HTTPException(
            status_code=409, detail={"reason": "operation_conflict"}
        ) from None
    fallback = {
        "invalid_request": "用户目录请求无效",
        "current_scope_busy": "用户目录正在更新，请刷新后重试",
    }
    raise HTTPException(
        status_code=error.status_code or 503,
        detail=error.detail or fallback.get(error.code, "身份目录服务暂不可用"),
    ) from None


async def execute_directory(request, credentials, commands, operation, payload):
    origin = audit_origin_from_request(request)
    try:
        command = decode_directory_request(
            {
                "schema": "aap-identity-directory/v1",
                "operation": operation,
                "operation_id": str(uuid.uuid4()),
                "token": credentials.credentials,
                "audit_context": {
                    "method": origin.method,
                    "path": origin.path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
                "payload": payload,
            }
        )
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="用户目录请求无效") from None
    try:
        reply = await commands.execute(command)
    except DirectoryFailure as error:
        _reject_directory_error(error)
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份目录服务暂不可用") from None
    admitted = getattr(request.state, "identity_principal", None)
    principal = reply.principal
    if (
        admitted is None
        or any(
            getattr(principal, name) != getattr(admitted, name)
            for name in ("actor_id", "credential_type", "credential_id", "generation")
        )
        or (
            principal.credential_type == "jwt"
            and principal.expires_at != admitted.expires_at
        )
    ):
        raise HTTPException(status_code=503, detail="身份目录服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None
    return reply.data.model_dump(mode="python")
