from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from aap_identity.suspension_contract import (
    NATIVE_SUSPENSION_PROTOCOL,
    SUSPENSION_PROTOCOL,
    decode_suspension_request,
)
from aap_identity.suspension_manager import (
    suspension_derivation_id,
    suspension_request_fingerprint,
)
from session_helpers import ACTOR_ID, audit_context, principal

SUSPENSION_SECRET = "identity-suspension-test-secret"
SUSPENSION_CONTROL = "identity-suspension-control-token"
TARGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION_ID = uuid.UUID("fedcba98-7654-4321-8fed-cba987654321")
OPAQUE_KEY_HASH = "legacy opaque api-key hash value"


def suspension_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID = TARGET_ID,
    token: str = "private-suspension-bearer",
    reason: str | None = None,
    audit: dict | None = None,
):
    payload = {"target_user_id": str(target_user_id)}
    if operation == "reactivate_account":
        payload["reason"] = reason
    return decode_suspension_request(
        {
            "schema": SUSPENSION_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or OPERATION_ID),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/users/lifecycle"),
            "payload": payload,
        }
    )


def suspension_user(**changes):
    value = {
        "id": TARGET_ID,
        "email": "target@example.test",
        "name": "Suspension target",
        "role": "employee",
        "group_name": "Historical group",
        "group_id": None,
        "status": "offline",
        "is_active": False,
        "disabled_kind": "suspended",
        "disabled_at": datetime(2026, 10, 20, tzinfo=UTC),
        "disabled_by": ACTOR_ID,
        "disabled_reason": "管理员停用账号",
        "email_verified_at": None,
        "last_login_at": None,
        "last_seen_at": None,
        "password_admin_reset_at": None,
        "deactivation_requested_at": datetime(2026, 10, 19, tzinfo=UTC),
        "deactivation_scheduled_at": datetime(2026, 10, 26, tzinfo=UTC),
        "preferences": {"opaque": {"numeric": 9007199254740993}},
        "avatar_ref": "legacy-avatar-reference",
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    value.update(changes)
    return value


def native_command(request, *, actor=None, key_hash=None):
    from aap_identity.suspension_contract import native_suspension_command

    actor = actor or principal(role="super_admin")
    return native_suspension_command(
        request,
        actor=actor,
        expected_key_hash=key_hash,
        request_fingerprint=suspension_request_fingerprint(SUSPENSION_SECRET, request),
        derivation_id=suspension_derivation_id(SUSPENSION_SECRET),
    )


def wire_success(request, user, *, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": SUSPENSION_PROTOCOL,
                "ok": True,
                "operation": request.operation,
                "operation_id": str(request.operation_id),
                "principal": (
                    current_principal or principal(role="super_admin")
                ).model_dump(mode="json"),
                "data": {"user": user},
            },
            default=str,
        )
    )


def native_success(command, user, *, replayed=False, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": NATIVE_SUSPENSION_PROTOCOL,
                "ok": True,
                "operation": command.operation,
                "operation_id": str(command.operation_id),
                "principal": (current_principal or command.actor).model_dump(
                    mode="json"
                ),
                "data": {"user": user},
                "replayed": replayed,
            },
            default=str,
        )
    )
