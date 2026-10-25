"""Canonical current-authority phases for Project read operations."""

from __future__ import annotations

import uuid

from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
)
from .project_query_contract import ProjectQueryRejected
from .project_query_values import decode_project_row
from .member_values import (
    ProjectMemberRowsUnavailable,
    decode_project_member_rows,
)


def _owner_id(row):
    return uuid.UUID(row["owner_id"]) if row["owner_id"] is not None else None


def _actor_id(request):
    admitted = getattr(request, "admitted_actor", None)
    return admitted.id if admitted is not None else request.actor_id


def _member_rows(request, facts):
    try:
        rows = decode_project_member_rows(
            facts.rows_json,
            target_id=request.target_id,
            user_id=_actor_id(request),
            max_rows=1,
        )
    except ProjectMemberRowsUnavailable:
        from .project_query_contract import ProjectQueryUnavailable

        raise ProjectQueryUnavailable() from None
    return rows[0] if rows else None


def _member_access(
    request, *, platform_role, membership_id, project_role, version=None
):
    try:
        return project_member_access(
            actor_id=_actor_id(request),
            platform_role=platform_role,
            project_id=request.target_id,
            membership_id=membership_id,
            project_role=project_role,
            membership_version=version,
        )
    except ProjectAccessPolicyFailure as error:
        raise ProjectQueryRejected(error.status_code, error.detail) from None


def _manager_access(request, *, platform_role, row) -> ProjectAccess | None:
    try:
        return project_manager_access(
            actor_id=_actor_id(request),
            platform_role=platform_role,
            project_id=request.target_id,
            owner_id=_owner_id(row),
        )
    except ProjectAccessPolicyFailure as error:
        raise ProjectQueryRejected(error.status_code, error.detail) from None


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

    access = _manager_access(request, platform_role=actor.platform_role, row=row)
    if access is None:
        if request.operation in (
            "get_project_access",
            "require_project_manager",
            "require_project_visible",
        ):
            member = _member_rows(
                request,
                await step(request, transaction, "access_member_rows"),
            )
            access = _member_access(
                request,
                platform_role=actor.platform_role,
                membership_id=member.id if member is not None else None,
                project_role=member.role if member is not None else None,
                version=member.version if member is not None else None,
            )
        else:
            member = (await step(request, transaction, "member_access")).member
            access = _member_access(
                request,
                platform_role=actor.platform_role,
                membership_id=member.id if member is not None else None,
                project_role=member.role if member is not None else None,
            )
    if request.operation in ("project_readiness", "require_project_manager") and (
        not access.is_manager
    ):
        raise ProjectQueryRejected(403, "仅项目负责人或超级管理员可执行")
    return row


async def current_project_access(request, transaction, step) -> ProjectAccess:
    """Resolve the endpoint's second actor/project/membership read phase."""

    actor = (await step(request, transaction, "actor_status")).actor
    if actor is None or not actor.is_active:
        raise ProjectQueryRejected(
            401, "账号已停用或不存在", {"WWW-Authenticate": "Bearer"}
        )

    facts = await step(request, transaction, "get_row")
    row = decode_project_row(facts.row_json, target_id=request.target_id)
    if row is None:
        raise ProjectQueryRejected(404, "项目不存在")

    manager = _manager_access(request, platform_role=actor.platform_role, row=row)
    if manager is not None:
        return manager

    member = _member_rows(
        request,
        await step(request, transaction, "access_member_rows"),
    )
    return _member_access(
        request,
        platform_role=actor.platform_role,
        membership_id=member.id if member is not None else None,
        project_role=member.role if member is not None else None,
        version=member.version if member is not None else None,
    )
