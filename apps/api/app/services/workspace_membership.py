"""Bind public project-member operations to the configured Workspace owner."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException
import httpx

from aap_workspace.config import secret_source
from aap_workspace.contract import (
    MembershipUnavailable,
    decode_membership_reply,
    decode_membership_request,
)
from aap_workspace.manager import MembershipManager
from aap_workspace.postgres import MembershipPostgresData
from app.config import settings
from app.middleware.request_id import request_id_var
from app.services.workspace_membership_data import WorkspaceMembershipData


class WorkspaceMembershipCommands:
    def __init__(self, db):
        self.db = db

    async def execute(self, request):
        try:
            async with asyncio.timeout(settings.workspace_membership_timeout_seconds):
                if settings.workspace_membership_url:
                    control = secret_source(
                        settings.workspace_membership_control_token,
                        settings.workspace_membership_control_token_file,
                    )
                    if not control:
                        raise MembershipUnavailable()
                    async with httpx.AsyncClient(
                        trust_env=False,
                        timeout=settings.workspace_membership_timeout_seconds,
                    ) as client:
                        response = await client.post(
                            settings.workspace_membership_url.rstrip("/")
                            + "/v1/workspace/membership/execute",
                            headers={"Authorization": f"Bearer {control}"},
                            json=request.model_dump(mode="json", by_alias=True),
                        )
                    if response.status_code == 500:
                        raise HTTPException(
                            status_code=500, detail="Internal Server Error"
                        )
                    if response.status_code != 200:
                        raise MembershipUnavailable()
                    reply = decode_membership_reply(request, response.content)
                else:
                    dsn = secret_source(
                        settings.workspace_membership_database_url,
                        settings.workspace_membership_database_url_file,
                    )
                    if dsn:
                        data = MembershipPostgresData(
                            dsn,
                            command_timeout=settings.workspace_membership_timeout_seconds,
                        )
                        await data.membership_policy()
                    else:
                        data = WorkspaceMembershipData(self.db)
                    reply = await MembershipManager(data).execute(request)
            if not reply.ok:
                raise HTTPException(
                    status_code=reply.error.status_code,
                    detail=reply.error.detail,
                    headers=reply.error.headers,
                )
            return reply.data
        except (
            MembershipUnavailable,
            httpx.HTTPError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise HTTPException(
                status_code=503, detail="项目成员服务暂不可用"
            ) from None


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
    result = await WorkspaceMembershipCommands(db).execute(
        _request(
            "add_member",
            project_id=project_id,
            actor_id=actor_id,
            payload={
                "target_user_id": str(target_user_id),
                "project_role": project_role,
            },
        )
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
    result = await WorkspaceMembershipCommands(db).execute(
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
        )
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
    result = await WorkspaceMembershipCommands(db).execute(
        _request(
            "change_role", project_id=project_id, actor_id=actor_id, payload=payload
        )
    )
    return result.member


async def remove_member(db, *, project_id, actor_id, member_id):
    await WorkspaceMembershipCommands(db).execute(
        _request(
            "remove_member",
            project_id=project_id,
            actor_id=actor_id,
            payload={"member_id": str(member_id)},
        )
    )
