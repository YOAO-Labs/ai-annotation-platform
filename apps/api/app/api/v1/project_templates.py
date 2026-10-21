"""Project template HTTP admission; Workspace owns reads and writes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from aap_workspace.configuration_requests import (
    ProjectTemplateCreate,
    ProjectTemplateOut,
    ProjectTemplateUpdate,
)
from app.services.workspace_configuration import execute_configuration
from app.services.workspace_template_query import execute_template_query
from app.services.identity_contracts import IdentityActorFacts


router = APIRouter()


async def _configuration_response(db, execution, actor):
    data = await execute_template_query(
        db,
        operation="get_template_projection",
        actor=actor,
        target_id=execution.resource_id,
        configuration_receipt={
            "operation_id": execution.operation_id,
            "operation": execution.operation,
        },
    )
    if data is None:
        verb = {
            "create_template": "Created",
            "update_template": "Updated",
            "duplicate_template": "Duplicated",
        }[execution.operation]
        raise RuntimeError(f"{verb} configuration template disappeared")
    return data


@router.get("", response_model=list[ProjectTemplateOut])
async def list_templates(
    scope: Annotated[
        str | None, Query(pattern="^(private|organization|public)$")
    ] = None,
    type_key: Annotated[list[str] | None, Query()] = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_template_query(
        db,
        operation="list_templates",
        actor=user,
        filters={"scope": scope, "type_key": type_key, "search": search},
    )


@router.post("", response_model=ProjectTemplateOut, status_code=201)
async def create_template(
    data: ProjectTemplateCreate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    result = await execute_configuration(
        db,
        operation="create_template",
        payload=data.model_dump(exclude_unset=True, exclude_none=False),
        actor_id=user.id,
    )
    await db.commit()
    return await _configuration_response(db, result, user)


@router.get("/{template_id}", response_model=ProjectTemplateOut)
async def get_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_template_query(
        db, operation="get_template", actor=user, target_id=template_id
    )


@router.patch("/{template_id}", response_model=ProjectTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    data: ProjectTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    result = await execute_configuration(
        db,
        operation="update_template",
        target_id=template_id,
        payload=data.model_dump(exclude_unset=True),
        actor_id=user.id,
    )
    await db.commit()
    return await _configuration_response(db, result, user)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    await execute_configuration(
        db, operation="delete_template", target_id=template_id, actor_id=user.id
    )
    await db.commit()


@router.post(
    "/{template_id}/duplicate", response_model=ProjectTemplateOut, status_code=201
)
async def duplicate_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    """克隆任意可见模板为当前用户私有模板."""
    result = await execute_configuration(
        db, operation="duplicate_template", target_id=template_id, actor_id=user.id
    )
    await db.commit()
    return await _configuration_response(db, result, user)
