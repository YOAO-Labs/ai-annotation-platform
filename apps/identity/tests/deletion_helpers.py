from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from aap_identity.deletion_contract import (
    DELETION_PROTOCOL,
    NATIVE_DELETION_PROTOCOL,
    decode_deletion_request,
)
from aap_identity.deletion_manager import deletion_derivation_id
from session_helpers import ACTOR_ID, audit_context, principal

DELETION_SECRET = "identity-deletion-test-secret"
DELETION_CONTROL = "identity-deletion-control-token"
TARGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RECEIVER_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPERATION_ID = uuid.UUID("fedcba98-7654-4321-8fed-cba987654321")
OPAQUE_KEY_HASH = "legacy opaque api-key hash value"


def deletion_request(
    *,
    operation_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID = TARGET_ID,
    transfer_to_user_id: uuid.UUID | None = None,
    token: str = "private-deletion-bearer",
    audit: dict | None = None,
):
    return decode_deletion_request(
        {
            "schema": DELETION_PROTOCOL,
            "operation": "delete_account",
            "operation_id": str(operation_id or OPERATION_ID),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/users/target"),
            "payload": {
                "target_user_id": str(target_user_id),
                "transfer_to_user_id": (
                    str(transfer_to_user_id) if transfer_to_user_id else None
                ),
            },
        }
    )


def deletion_user(**changes):
    value = {
        "id": TARGET_ID,
        "email": "target@example.test",
        "name": "Deleted user",
        "role": "employee",
        "group_name": None,
        "group_id": None,
        "status": "offline",
        "is_active": False,
        "disabled_kind": "deleted",
        "disabled_at": datetime(2026, 10, 20, tzinfo=UTC),
        "disabled_by": ACTOR_ID,
        "disabled_reason": "管理员删除账号",
        "email_verified_at": None,
        "last_login_at": None,
        "last_seen_at": None,
        "password_admin_reset_at": None,
        "deactivation_requested_at": None,
        "deactivation_scheduled_at": None,
        "preferences": {"opaque": {"numeric": 9007199254740993}},
        "avatar_ref": None,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    value.update(changes)
    return value


def native_command(request=None, *, actor=None, key_hash=None):
    from aap_identity.deletion_contract import native_deletion_command

    from aap_identity.deletion_manager import deletion_request_fingerprint

    request = request or deletion_request()
    actor = actor or principal(role="super_admin")
    return native_deletion_command(
        request,
        actor=actor,
        expected_key_hash=key_hash,
        request_fingerprint=deletion_request_fingerprint(DELETION_SECRET, request),
        derivation_id=deletion_derivation_id(DELETION_SECRET),
    )


def wire_success(request, user, *, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": DELETION_PROTOCOL,
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
                "schema": NATIVE_DELETION_PROTOCOL,
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
