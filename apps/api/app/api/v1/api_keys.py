"""Public API-key commands consumed through the independent Identity owner."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.key_contract import KeyFailure, decode_key_request
from aap_identity.contract import Principal
from app.deps import bearer_scheme, get_current_principal, get_db
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate
from app.services.api_key_contracts import ApiKeyManagementPort
from app.services.api_key_service import IdentityKeyManagement
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway

router = APIRouter()
_UNAUTHENTICATED = {
    "credential_invalid",
    "credential_revoked",
    "generation_outdated",
    "token_expired",
    "user_inactive",
    "invalid_account_role",
}


def get_api_key_management(
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> ApiKeyManagementPort:
    return IdentityKeyManagement(db, identity)


def _unauthenticated():
    raise HTTPException(
        status_code=401, detail="无效的认证凭据", headers={"WWW-Authenticate": "Bearer"}
    )


async def _execute(request, credentials, api_keys, operation, payload):
    token = credentials.credentials
    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        _unauthenticated()
    try:
        command = decode_key_request(
            {
                "schema": "aap-identity-keys/v1",
                "operation": operation,
                "operation_id": str(uuid.uuid4()),
                "token": token,
                "payload": payload,
            }
        )
        reply = await api_keys.execute(command)
    except KeyFailure as error:
        if error.code in _UNAUTHENTICATED:
            _unauthenticated()
        if error.code == "key_not_found":
            raise HTTPException(
                status_code=404, detail="API key 不存在或已吊销"
            ) from None
        if error.code == "operation_conflict":
            raise HTTPException(
                status_code=409, detail={"reason": "operation_conflict"}
            ) from None
        raise HTTPException(status_code=503, detail="身份凭据服务暂不可用") from None
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份凭据服务暂不可用") from None
    principal = reply.principal
    admitted = getattr(request.state, "identity_principal", None)
    if (
        admitted is None
        or any(
            getattr(principal, field) != getattr(admitted, field)
            for field in ("actor_id", "credential_type", "credential_id", "generation")
        )
        or (
            principal.credential_type == "jwt"
            and principal.expires_at != admitted.expires_at
        )
    ):
        raise HTTPException(status_code=503, detail="身份凭据服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None
    return reply.model_dump(mode="python", by_alias=True)["data"]


@router.get("", response_model=list[ApiKeyOut])
async def list_my_keys(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    data = await _execute(request, credentials, api_keys, "list_api_keys", {})
    return data["items"]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_my_key(
    request: Request,
    data: ApiKeyCreate,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    result = await _execute(
        request, credentials, api_keys, "create_api_key", data.model_dump(mode="json")
    )
    return {**result["key"], "plaintext": result["plaintext"]}


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def update_my_key(
    key_id: uuid.UUID,
    request: Request,
    data: ApiKeyUpdate,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    result = await _execute(
        request,
        credentials,
        api_keys,
        "update_api_key",
        {
            "key_id": str(key_id),
            "patch": data.model_dump(mode="json", exclude_unset=True),
        },
    )
    return result["key"]


@router.post("/{key_id}/rotate", response_model=ApiKeyCreated)
async def rotate_my_key(
    key_id: uuid.UUID,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    result = await _execute(
        request, credentials, api_keys, "rotate_api_key", {"key_id": str(key_id)}
    )
    return {**result["key"], "plaintext": result["plaintext"]}


@router.delete("/{key_id}", status_code=204)
async def revoke_my_key(
    key_id: uuid.UUID,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    await _execute(
        request, credentials, api_keys, "revoke_api_key", {"key_id": str(key_id)}
    )
