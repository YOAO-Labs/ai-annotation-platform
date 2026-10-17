"""v0.9.3 · /me/api-keys CRUD（v0.15.11 加 rotate / patch / 过期）。

- list/create/revoke/rotate/patch 均针对当前登录用户自己的 keys，不分角色。
- create / rotate 响应一次性返回 plaintext，前端必须当场展示并提示复制；之后无法再获取。
- revoke 是软删（revoked_at 落时间戳），不删行，方便审计追溯 last_used_at。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user_id, get_db, require_active_task_actor
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate
from app.services.api_key_contracts import ApiKeyManagementPort
from app.services.api_key_service import SqlApiKeyManagement

router = APIRouter(dependencies=[Depends(require_active_task_actor)])


def get_api_key_management(
    db: AsyncSession = Depends(get_db),
) -> ApiKeyManagementPort:
    """Bind API key management to the request's existing SQL session."""
    return SqlApiKeyManagement(db)


@router.get("", response_model=list[ApiKeyOut])
async def list_my_keys(
    owner_id: uuid.UUID = Depends(get_current_user_id),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    return await api_keys.list_keys(owner_id)


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_my_key(
    data: ApiKeyCreate,
    owner_id: uuid.UUID = Depends(get_current_user_id),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    return await api_keys.create_key(owner_id, data)


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def update_my_key(
    key_id: uuid.UUID,
    data: ApiKeyUpdate,
    owner_id: uuid.UUID = Depends(get_current_user_id),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    updated = await api_keys.update_key(owner_id, key_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail="API key 不存在或已吊销")
    return updated


@router.post("/{key_id}/rotate", response_model=ApiKeyCreated)
async def rotate_my_key(
    key_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(get_current_user_id),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    rotated = await api_keys.rotate_key(owner_id, key_id)
    if rotated is None:
        raise HTTPException(status_code=404, detail="API key 不存在或已吊销")
    return rotated


@router.delete("/{key_id}", status_code=204)
async def revoke_my_key(
    key_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(get_current_user_id),
    api_keys: ApiKeyManagementPort = Depends(get_api_key_management),
):
    if not await api_keys.revoke_key(owner_id, key_id):
        raise HTTPException(status_code=404, detail="API key 不存在或已吊销")
