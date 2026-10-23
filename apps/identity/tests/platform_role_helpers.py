from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from aap_identity.platform_role_contract import (
    LABELING_PLATFORM_ROLE_FACTS_PROTOCOL,
    NATIVE_PLATFORM_ROLE_PROTOCOL,
    PLATFORM_ROLE_PROTOCOL,
    WORKSPACE_PLATFORM_ROLE_FACTS_PROTOCOL,
    decode_platform_role_request,
    native_platform_role_command,
)
from aap_identity.platform_role_manager import (
    platform_role_derivation_id,
    platform_role_request_fingerprint,
)
from session_helpers import audit_context, principal

PLATFORM_ROLE_SECRET = "platform-role-test-signing-secret"
CONTROL = "identity-platform-role-runtime-control-token"
TARGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION_ID = uuid.UUID("fedcba98-7654-4321-8fed-cba987654321")
PROJECT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPAQUE_KEY_HASH = "legacy opaque api-key hash value"


def platform_role_request(
    operation: str,
    *,
    role: str = "employee",
    operation_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID = TARGET_ID,
    token: str = "platform-role-test-bearer",
    audit: dict | None = None,
):
    return decode_platform_role_request(
        {
            "schema": PLATFORM_ROLE_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or OPERATION_ID),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/users/role"),
            "payload": {"target_user_id": str(target_user_id), "role": role},
        }
    )


def role_preview_data(**changes):
    values = {
        "user_id": TARGET_ID,
        "email": "target@example.test",
        "current_role": "historical_global_role",
        "requested_role": "employee",
        "can_change": True,
        "blockers": [],
        "projects": [],
        "assigned_batch_count": 0,
        "assigned_task_count": 0,
        "review_task_count": 0,
        "other_project_count": 0,
        "warnings": [
            "平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。",
            "若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。",
        ],
    }
    values.update(changes)
    return values


def role_project(**changes):
    values = {
        "project_id": PROJECT_ID,
        "project_name": "Example project",
        "membership_role": "annotator",
        "annotator_batch_count": 1,
        "reviewer_batch_count": 2,
        "assigned_task_count": 3,
        "review_task_count": 4,
    }
    values.update(changes)
    return values


def role_user(**changes):
    values = {
        "id": TARGET_ID,
        "email": "target@example.test",
        "name": "Role target",
        "role": "employee",
        "group_name": "Historical group",
        "group_id": None,
        "status": "historical_status",
        "is_active": True,
        "disabled_kind": None,
        "disabled_at": None,
        "disabled_by": None,
        "disabled_reason": None,
        "email_verified_at": None,
        "last_login_at": None,
        "last_seen_at": None,
        "password_admin_reset_at": None,
        "deactivation_requested_at": None,
        "deactivation_scheduled_at": None,
        "preferences": {"opaque": {"numeric": 9007199254740993}},
        "avatar_ref": "legacy-avatar-reference",
        "created_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    values.update(changes)
    return values


def native_command(request, *, actor=None, key_hash=None):
    actor = actor or principal(role="super_admin")
    return native_platform_role_command(
        request,
        actor=actor,
        expected_key_hash=key_hash,
        request_fingerprint=platform_role_request_fingerprint(
            PLATFORM_ROLE_SECRET, request
        ),
        derivation_id=platform_role_derivation_id(PLATFORM_ROLE_SECRET),
    )


def wire_success(request, data, *, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": PLATFORM_ROLE_PROTOCOL,
                "ok": True,
                "operation": request.operation,
                "operation_id": str(request.operation_id),
                "principal": (
                    current_principal or principal(role="super_admin")
                ).model_dump(mode="json"),
                "data": data,
            },
            default=str,
        )
    )


def native_success(command, data, *, replayed=False, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": NATIVE_PLATFORM_ROLE_PROTOCOL,
                "ok": True,
                "operation": command.operation,
                "operation_id": str(command.operation_id),
                "principal": (current_principal or command.actor).model_dump(
                    mode="json"
                ),
                "data": data,
                "replayed": replayed,
            },
            default=str,
        )
    )


def workspace_facts(**changes):
    values = {
        "schema": WORKSPACE_PLATFORM_ROLE_FACTS_PROTOCOL,
        "target_user_id": str(TARGET_ID),
        "owned_project_count": 1,
        "non_viewer_membership_count": 1,
        "membership_projects": [
            {
                "project_id": str(PROJECT_ID),
                "project_name": "Example project",
                "membership_role": "annotator",
            }
        ],
    }
    values.update(changes)
    return values


def labeling_facts(**changes):
    values = {
        "schema": LABELING_PLATFORM_ROLE_FACTS_PROTOCOL,
        "target_user_id": str(TARGET_ID),
        "unfinished_effective_work_count": 5,
        "membership_projects": [
            {
                "project_id": str(PROJECT_ID),
                "annotator_batch_count": 1,
                "reviewer_batch_count": 2,
                "assigned_task_count": 3,
                "review_task_count": 4,
            }
        ],
    }
    values.update(changes)
    return values
