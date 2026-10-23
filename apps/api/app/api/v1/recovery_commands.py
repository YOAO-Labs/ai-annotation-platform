"""HTTP composition for anonymous and scoped Identity credential commands."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.recovery_contract import RecoveryFailure, decode_recovery_request
from app.deps import get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from app.services.identity_recovery_commands import (
    IdentityRecoveryCommands,
    RecoveryCommandsPort,
)


def get_recovery_commands(
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> RecoveryCommandsPort:
    return IdentityRecoveryCommands(db, identity)


async def read_registration_status(
    commands: RecoveryCommandsPort, *, deployment_default: bool = False
) -> bool:
    try:
        value = await commands.registration_status(
            deployment_default=deployment_default
        )
        if type(value) is not bool:
            raise IdentityGatewayUnavailable()
        return value
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份恢复服务暂不可用") from None


async def get_registration_default(
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
) -> bool:
    return await read_registration_status(commands, deployment_default=True)


def _reject_recovery_error(error, operation):
    if error.code == "registration_disabled":
        raise HTTPException(status_code=403, detail="开放注册未启用") from None
    if error.code == "email_exists":
        raise HTTPException(status_code=409, detail="该邮箱已被注册") from None
    if error.code == "captcha_required":
        raise HTTPException(status_code=400, detail="captcha_failed") from None
    if error.code == "policy_rejected":
        raise HTTPException(status_code=422, detail=error.detail) from None
    if error.code == "operation_conflict":
        raise HTTPException(
            status_code=409, detail={"reason": "operation_conflict"}
        ) from None
    if error.code == "invalid_request":
        detail = {
            "consume_password_reset": "重置链接无效或已过期",
            "consume_email_verification": "验证链接无效或已过期",
        }.get(operation, "身份请求无效")
        raise HTTPException(status_code=400, detail=detail) from None
    raise HTTPException(status_code=503, detail="身份恢复服务暂不可用") from None


async def execute_recovery(request, commands, operation, payload, *, operation_id=None):
    origin = audit_origin_from_request(request)
    try:
        command = decode_recovery_request(
            {
                "schema": "aap-identity-recovery/v1",
                "operation": operation,
                "operation_id": str(operation_id or uuid.uuid4()),
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
        detail = {
            "consume_password_reset": "重置链接无效或已过期",
            "consume_email_verification": "验证链接无效或已过期",
        }.get(operation, "身份请求无效")
        raise HTTPException(status_code=400, detail=detail) from None
    try:
        reply = await commands.execute(command)
    except RecoveryFailure as error:
        _reject_recovery_error(error, operation)
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份恢复服务暂不可用") from None
    return reply.data.model_dump(mode="python")
