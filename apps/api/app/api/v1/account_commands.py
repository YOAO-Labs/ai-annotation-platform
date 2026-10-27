"""HTTP composition for Identity self-account value commands."""

from __future__ import annotations

import uuid
from math import isfinite

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.account_contract import AccountFailure, decode_account_request
from aap_identity.contract import Principal
from app.deps import get_current_principal, get_db
from app.middleware.request_id import request_id_var
from app.services.audit import audit_origin_from_request
from app.services.identity_account_commands import (
    AccountCommandsPort,
    IdentityAccountCommands,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway


def get_account_commands(
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> AccountCommandsPort:
    return IdentityAccountCommands(db, identity, expected_principal=principal)


def _reject_account_error(error: AccountFailure):
    if error.code == "policy_rejected":
        raise HTTPException(
            status_code=error.status_code, detail=error.detail
        ) from None
    if error.code == "account_busy":
        raise HTTPException(
            status_code=409, detail="账号正在更新，请刷新后重试"
        ) from None
    if error.code == "operation_conflict":
        raise HTTPException(
            status_code=409, detail={"reason": "operation_conflict"}
        ) from None
    if error.code in {
        "avatar_reference_changed",
        "avatar_receipt_invalid",
        "storage_maintenance",
        "storage_maintenance_changed",
        "storage_maintenance_busy",
    }:
        raise HTTPException(status_code=409, detail={"code": error.code}) from None
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
    raise HTTPException(status_code=503, detail="身份账户服务暂不可用") from None


def _nonfinite_preference_errors(value, path=()):
    """Locate numbers the public JSON parser cannot encode on the owner wire."""
    if isinstance(value, float) and not isfinite(value):
        yield {
            "type": "value_error",
            "loc": ("body", *path),
            "msg": "Value error, preferences must contain finite JSON values",
        }
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _nonfinite_preference_errors(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _nonfinite_preference_errors(item, (*path, index))


async def execute_account(
    request, credentials, commands, operation, payload, *, operation_id=None
):
    origin = audit_origin_from_request(request)
    try:
        command = decode_account_request(
            {
                "schema": "aap-identity-accounts/v1",
                "operation": operation,
                "operation_id": str(operation_id or uuid.uuid4()),
                "token": credentials.credentials,
                "payload": payload,
                "audit_context": {
                    "method": origin.method,
                    "path": origin.path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
            }
        )
    except (ValueError, TypeError, UnicodeError):
        if operation == "patch_preferences":
            errors = list(_nonfinite_preference_errors(payload.get("patch", {})))
            if errors:
                raise HTTPException(status_code=422, detail=errors) from None
        raise HTTPException(status_code=422, detail="账户请求无效") from None
    try:
        reply = await commands.execute(command)
    except AccountFailure as error:
        _reject_account_error(error)
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份账户服务暂不可用") from None
    admitted = getattr(request.state, "identity_principal", None)
    principal = reply.principal
    if (
        admitted is None
        or any(
            getattr(principal, name) != getattr(admitted, name)
            for name in (
                "actor_id",
                "credential_type",
                "credential_id",
                "generation",
            )
        )
        or (
            principal.credential_type == "jwt"
            and principal.expires_at != admitted.expires_at
        )
    ):
        raise HTTPException(status_code=503, detail="身份账户服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None
    return reply.data.model_dump(mode="python")
