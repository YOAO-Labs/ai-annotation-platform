"""Portable policy for saved, named inference pipelines."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from .pipeline_contract import PipelineActor, PipelineRejected, PipelineRow

MANAGER_ROLES = frozenset({"super_admin", "project_admin"})
PIPELINE_SCOPES = frozenset({"private", "organization", "public"})


def validate_pipeline_scope_owner(
    scope: str | None,
    project_id: UUID | None,
    organization_id: UUID | None,
) -> None:
    """Preserve the public schema's established scope/owner pair validation."""
    if scope == "private":
        if project_id is None or organization_id is not None:
            raise ValueError(
                "private 编排必须指定 project_id 且不能指定 organization_id"
            )
    elif scope == "organization":
        if project_id is not None or organization_id is None:
            raise ValueError(
                "organization 编排必须指定 organization_id 且不能指定 project_id"
            )
    elif project_id is not None or organization_id is not None:
        raise ValueError("public 编排不能指定 project_id / organization_id")


def assert_pipeline_creator_role(actor: PipelineActor) -> None:
    if actor.role not in MANAGER_ROLES:
        raise PipelineRejected(403, "仅项目管理员或超级管理员可创建编排")


async def assert_can_create_pipeline_scope(
    scope: str | None,
    organization_id: UUID | None,
    actor: PipelineActor,
    organization_ids: Callable[[], Awaitable[list[UUID]]],
) -> None:
    """Return the original create-scope error, keeping org reads at their phase."""
    if scope == "public" and actor.role != "super_admin":
        raise PipelineRejected(403, "仅超级管理员可创建公共编排")
    if scope != "organization":
        return None
    if organization_id is None:
        raise PipelineRejected(400, "organization scope 必须指定 organization_id")
    if actor.role == "super_admin":
        return
    if organization_id not in await organization_ids():
        raise PipelineRejected(403, "无权创建该组织编排")


async def assert_pipeline_visible(
    pipeline: PipelineRow,
    actor: PipelineActor,
    organization_ids: Callable[[], Awaitable[list[UUID]]],
) -> None:
    """Hide private and organization resources with the legacy 404 policy."""
    if actor.role == "super_admin" or pipeline.scope == "public":
        return
    if pipeline.scope == "private" and pipeline.created_by == actor.id:
        return
    if pipeline.scope == "organization" and pipeline.organization_id is not None:
        if pipeline.organization_id in await organization_ids():
            return
    raise PipelineRejected(404, "编排不存在")


async def can_edit_pipeline(
    pipeline: PipelineRow,
    actor: PipelineActor,
    organization_ids: Callable[[], Awaitable[list[UUID]]],
) -> bool:
    if actor.role == "super_admin" or pipeline.created_by == actor.id:
        return True
    if pipeline.scope == "organization" and pipeline.organization_id is not None:
        return pipeline.organization_id in await organization_ids()
    return False


def copy_pipeline_stages(stages: Any) -> Any:
    """Match the old deepcopy boundary, including JSON null and scalar values."""
    return deepcopy(stages)
