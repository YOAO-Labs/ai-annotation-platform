"""HTTP composition for the selected Identity invitation owner."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import Principal
from aap_identity.invitation_contract import (
    InvitationFailure,
    decode_invitation_request,
)
from app.deps import get_current_principal, get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from app.services.identity_invitation_commands import (
    IdentityInvitationCommands,
    InvitationCommandsPort,
)

_PUBLIC_OPERATIONS = {"resolve_invitation", "register_invitation"}


def require_invitation_manager(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role not in {"super_admin", "project_admin"}:
        raise HTTPException(
            status_code=403, detail="需要角色权限: super_admin或project_admin"
        )
    return principal


def get_invitation_commands(
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> InvitationCommandsPort:
    return IdentityInvitationCommands(db, identity)


def get_authenticated_invitation_commands(
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> InvitationCommandsPort:
    return IdentityInvitationCommands(db, identity, expected_principal=principal)


def get_invitation_manager_commands(
    principal: Principal = Depends(require_invitation_manager),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> InvitationCommandsPort:
    return IdentityInvitationCommands(db, identity, expected_principal=principal)


def _reject_invitation_error(error: InvitationFailure, payload=None):
    if error.code == "operation_conflict":
        raise HTTPException(
            status_code=409, detail={"reason": "operation_conflict"}
        ) from None
    if error.code == "mail_failed":
        raise HTTPException(
            status_code=502,
            detail={
                "code": "invitation_email_failed",
                "message": error.detail,
                "invitation_id": str(payload["invitation_id"]),
            },
        ) from None
    raise HTTPException(
        status_code=error.status_code or 503,
        detail=error.detail or "身份邀请服务暂不可用",
        headers={"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None,
    ) from None


def _bind_invitation_principal(request, reply, operation):
    if operation in _PUBLIC_OPERATIONS:
        if reply.principal is not None:
            raise HTTPException(
                status_code=503, detail="身份邀请服务返回了不匹配的主体"
            )
        return
    admitted = getattr(request.state, "identity_principal", None)
    principal = reply.principal
    if (
        admitted is None
        or principal is None
        or any(
            getattr(principal, name) != getattr(admitted, name)
            for name in ("actor_id", "credential_type", "credential_id", "generation")
        )
        or (
            principal.credential_type == "jwt"
            and principal.expires_at != admitted.expires_at
        )
    ):
        raise HTTPException(status_code=503, detail="身份邀请服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None


def _mail_failure(data):
    code = data.get("mail_failure_code")
    if code == "not_configured":
        message = "SMTP 未完整配置（host / port / from 必填）"
    elif code == "message_invalid":
        message = "SMTP 邮件内容无效"
    elif code:
        message = f"SMTP 发送失败: {code}"
    else:
        message = "邀请邮件发送结果暂未确认，请稍后重试"
    raise HTTPException(
        status_code=502,
        detail={
            "code": "invitation_email_failed",
            "message": message,
            "invitation_id": str(data["invitation_id"]),
        },
    )


async def execute_invitation(
    request, credentials, commands, operation, payload, *, operation_id=None
):
    if operation in {"resolve_invitation", "register_invitation", "accept_invitation"}:
        proof = payload.get("invitation_token")
        if isinstance(proof, str) and not 1 <= len(proof) <= 64:
            raise HTTPException(status_code=404, detail="邀请链接无效")
    origin = audit_origin_from_request(request)
    audit_path = origin.path
    if operation == "resolve_invitation":
        audit_path = audit_path.rsplit("/", 1)[0] + "/{token}"
    try:
        command = decode_invitation_request(
            {
                "schema": "aap-identity-invitations/v1",
                "operation": operation,
                "operation_id": str(operation_id or uuid.uuid4()),
                "token": credentials.credentials if credentials is not None else None,
                "payload": payload,
                "audit_context": {
                    "method": origin.method,
                    "path": audit_path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
            }
        )
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="邀请请求无效") from None
    try:
        reply = await commands.execute(command)
    except InvitationFailure as error:
        _reject_invitation_error(error, payload)
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份邀请服务暂不可用") from None
    _bind_invitation_principal(request, reply, operation)
    data = reply.data.model_dump(mode="python")
    if operation == "send_invitation_email":
        if data["mail_status"] != "sent":
            _mail_failure(data)
        if data["ok"] is not True:
            raise HTTPException(status_code=503, detail="身份邀请服务返回了无效结果")
        for name in ("mail_status", "mail_failure_code"):
            data.pop(name, None)
    return data
