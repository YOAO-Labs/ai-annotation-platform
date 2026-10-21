"""Canonical current-authority phases for Project read operations."""

from __future__ import annotations

from .access_contract import (
    MANAGER_PLATFORM_ROLES,
    PLATFORM_ROLE_EMPLOYEE,
    PLATFORM_ROLE_VIEWER,
    PROJECT_ROLES,
)
from .project_query_contract import ProjectQueryRejected
from .project_query_values import decode_project_row


async def accessible_project_row(request, transaction, step):
    """Resolve the same Project -> actor -> refreshed Project -> membership order."""
    facts = await step(request, transaction, "get_row")
    if decode_project_row(facts.row_json, target_id=request.target_id) is None:
        raise ProjectQueryRejected(404, "项目不存在")

    actor = (await step(request, transaction, "actor_status")).actor
    if actor is None or not actor.is_active:
        raise ProjectQueryRejected(
            401, "账号已停用或不存在", {"WWW-Authenticate": "Bearer"}
        )

    facts = await step(request, transaction, "get_row")
    row = decode_project_row(facts.row_json, target_id=request.target_id)
    if row is None:
        raise ProjectQueryRejected(404, "项目不存在")

    platform_role = actor.platform_role
    if platform_role not in MANAGER_PLATFORM_ROLES | {
        PLATFORM_ROLE_EMPLOYEE,
        PLATFORM_ROLE_VIEWER,
    }:
        raise ProjectQueryRejected(403, "账号角色无效，访问被拒绝")
    manager = platform_role == "super_admin" or (
        platform_role in MANAGER_PLATFORM_ROLES
        and row["owner_id"] == str(actor.actor_id)
    )
    if not manager:
        member = (await step(request, transaction, "member_access")).member
        if member is None:
            raise ProjectQueryRejected(404, "项目不存在")
        if member.role not in PROJECT_ROLES:
            raise ProjectQueryRejected(403, "项目职责无效，访问被拒绝")
        if platform_role == PLATFORM_ROLE_VIEWER and member.role != "viewer":
            raise ProjectQueryRejected(403, "账号平台角色与项目职责不兼容")
    if request.operation == "project_readiness" and not manager:
        raise ProjectQueryRejected(403, "仅项目负责人或超级管理员可执行")
    return row
