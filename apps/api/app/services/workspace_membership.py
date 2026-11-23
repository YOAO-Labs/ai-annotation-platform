"""Run public project-member operations on the caller's Workspace transaction."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException

from yoao_workspace.contract import (
    MembershipUnavailable,
    decode_membership_request,
)
from yoao_workspace.manager import MembershipManager
from app.config import settings
from app.middleware.request_id import request_id_var
from app.services.workspace_membership_data import WorkspaceMembershipData


async def _execute(db, request):
    try:
        async with asyncio.timeout(settings.workspace_membership_timeout_seconds):
            reply = await MembershipManager(WorkspaceMembershipData(db)).execute(
                request
            )
    except (MembershipUnavailable, OSError, TimeoutError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="项目成员服务暂不可用") from None
    if not reply.ok:
        raise HTTPException(
            status_code=reply.error.status_code,
            detail=reply.error.detail,
            headers=reply.error.headers,
        )
    return reply.data


def _request(operation, *, project_id, actor_id, payload):
    return decode_membership_request(
        {
            "schema": "aap-workspace-membership/v1",
            "operation_id": str(uuid.uuid4()),
            "operation": operation,
            "actor_id": str(actor_id),
            "project_id": str(project_id),
            "payload": payload,
            "audit_context": {
                "method": None,
                "path": None,
                "ip": None,
                "request_id": request_id_var.get() or None,
            },
        }
    )


async def add_member(db, *, project_id, actor_id, target_user_id, project_role):
    result = await _execute(
        db,
        _request(
            "add_member",
            project_id=project_id,
            actor_id=actor_id,
            payload={
                "target_user_id": str(target_user_id),
                "project_role": project_role,
            },
        ),
    )
    return result.member


def _role_payload(
    member_id, target_role, replacement_annotator_id, replacement_reviewer_id
):
    return {
        "member_id": str(member_id),
        "target_role": target_role,
        "replacement_annotator_id": str(replacement_annotator_id)
        if replacement_annotator_id is not None
        else None,
        "replacement_reviewer_id": str(replacement_reviewer_id)
        if replacement_reviewer_id is not None
        else None,
    }


async def preview_role_change(
    db,
    *,
    project_id,
    actor_id,
    member_id,
    target_role,
    replacement_annotator_id=None,
    replacement_reviewer_id=None,
):
    result = await _execute(
        db,
        _request(
            "preview_role_change",
            project_id=project_id,
            actor_id=actor_id,
            payload=_role_payload(
                member_id,
                target_role,
                replacement_annotator_id,
                replacement_reviewer_id,
            ),
        ),
    )
    return result.model_dump(mode="python")


async def change_role(
    db,
    *,
    project_id,
    actor_id,
    member_id,
    target_role,
    expected_version,
    preview_token,
    reason,
    replacement_annotator_id=None,
    replacement_reviewer_id=None,
):
    payload = _role_payload(
        member_id, target_role, replacement_annotator_id, replacement_reviewer_id
    )
    payload.update(
        expected_version=expected_version, preview_token=preview_token, reason=reason
    )
    result = await _execute(
        db,
        _request(
            "change_role", project_id=project_id, actor_id=actor_id, payload=payload
        ),
    )
    return result.member


async def remove_member(db, *, project_id, actor_id, member_id):
    await _execute(
        db,
        _request(
            "remove_member",
            project_id=project_id,
            actor_id=actor_id,
            payload={"member_id": str(member_id)},
        ),
    )
