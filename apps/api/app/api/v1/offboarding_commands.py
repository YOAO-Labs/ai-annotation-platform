"""HTTP composition for Identity's versioned offboarding commands."""

from __future__ import annotations

import uuid

from aap_identity.contract import Principal
from aap_identity.offboarding_contract import (
    OffboardingFailure,
    decode_offboarding_request,
)
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_principal, get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from app.services.identity_offboarding_commands import (
    IdentityOffboardingCommands,
    OffboardingCommandsPort,
)


def require_offboarding_manager(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role not in {"super_admin", "project_admin"}:
        raise HTTPException(
            status_code=403, detail="需要角色权限: super_admin或project_admin"
        )
    return principal


def get_offboarding_commands(
    principal: Principal = Depends(require_offboarding_manager),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> OffboardingCommandsPort:
    return IdentityOffboardingCommands(db, identity, expected_principal=principal)


def _reject_offboarding_error(error: OffboardingFailure):
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
            "离职交接请求无效"
            if error.code == "invalid_request"
            else "身份生命周期服务暂不可用"
        ),
    ) from None


async def execute_offboarding(
    request, credentials, commands, operation, user_id, payload=None
):
    origin = audit_origin_from_request(request)
    try:
        command = decode_offboarding_request(
            {
                "schema": "aap-identity-offboarding/v1",
                "operation": operation,
                "operation_id": str(uuid.uuid4()),
                "token": credentials.credentials,
                "audit_context": {
                    "method": origin.method,
                    "path": origin.path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
                "payload": {"target_user_id": str(user_id), **(payload or {})},
            }
        )
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="离职交接请求无效") from None
    try:
        reply = await commands.execute(command)
    except OffboardingFailure as error:
        _reject_offboarding_error(error)
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
    field = "preview" if operation == "preview_offboarding" else "result"
    return reply.data.model_dump(mode="python")[field]
