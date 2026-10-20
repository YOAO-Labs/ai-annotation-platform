from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.api.v1.group_commands import execute_group, get_group_commands
from app.deps import bearer_scheme
from app.schemas.group import GroupCreate, GroupOut, GroupUpdate
from app.schemas.management import GroupPage
from app.services.identity_group_commands import GroupCommandsPort

router = APIRouter()


@router.get("", response_model=list[GroupOut])
async def list_groups(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    data = await execute_group(request, credentials, commands, "list_groups", {})
    return data["items"]


@router.get("/query", response_model=GroupPage)
async def query_groups(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    if page < 1 or page_size < 1 or page_size > 500:
        raise HTTPException(status_code=422, detail="分页参数超出范围")
    return await execute_group(
        request,
        credentials,
        commands,
        "query_groups",
        {"page": page, "page_size": page_size, "search": search},
    )


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    data = await execute_group(
        request, credentials, commands, "create_group", payload.model_dump()
    )
    return data["group"]


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    data = await execute_group(
        request,
        credentials,
        commands,
        "update_group",
        {"group_id": str(group_id), **payload.model_dump()},
    )
    return data["group"]


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    await execute_group(
        request, credentials, commands, "delete_group", {"group_id": str(group_id)}
    )
