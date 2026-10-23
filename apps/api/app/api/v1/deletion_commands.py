"""HTTP composition for Identity's account-deletion command."""

from __future__ import annotations

import uuid

from aap_identity.contract import Principal
from aap_identity.deletion_contract import DeletionFailure, decode_deletion_request
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_principal, get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_deletion_commands import (
    DeletionCommandsPort,
    IdentityDeletionCommands,
)
from app.services.identity_gateway import IdentityGateway, get_identity_gateway


def require_deletion_manager(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role not in {"super_admin", "project_admin"}:
        raise HTTPException(
            status_code=403, detail="需要角色权限: super_admin或project_admin"
        )
    return principal


def get_deletion_commands(
    principal: Principal = Depends(require_deletion_manager),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> DeletionCommandsPort:
    return IdentityDeletionCommands(db, identity, expected_principal=principal)


def _reject_deletion_error(error: DeletionFailure):
    # Admission succeeded; an intervening suspension retains the manager error.
    if error.code == "user_inactive":
        raise HTTPException(status_code=403, detail="当前账号已无管理权限") from None
    if error.code in {
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
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
    raise HTTPException(
        status_code=error.status_code or 503,
        detail=error.detail
        or (
            "账号删除请求无效"
            if error.code == "invalid_request"
            else "身份生命周期服务暂不可用"
        ),
    ) from None


async def execute_deletion(
    request, credentials, commands, user_id, transfer_to_user_id=None
):
    try:
        target_id = uuid.UUID(str(user_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="用户不存在") from None
    origin = audit_origin_from_request(request)
    try:
        command = decode_deletion_request(
            {
                "schema": "aap-identity-deletions/v1",
                "operation": "delete_account",
                "operation_id": str(uuid.uuid4()),
                "token": credentials.credentials,
                "audit_context": {
                    "method": origin.method,
                    "path": origin.path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
                "payload": {
                    "target_user_id": str(target_id),
                    "transfer_to_user_id": (
                        str(transfer_to_user_id)
                        if transfer_to_user_id is not None
                        else None
                    ),
                },
            }
        )
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="账号删除请求无效") from None
    try:
        reply = await commands.execute(command)
    except DeletionFailure as error:
        _reject_deletion_error(error)
    except IdentityGatewayUnavailable:
        raise HTTPException(
            status_code=503, detail="身份生命周期服务暂不可用"
        ) from None
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
        raise HTTPException(status_code=503, detail="身份服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None
    return reply.data.user.model_dump(mode="python")
