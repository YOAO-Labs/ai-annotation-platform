from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aap_identity.directory_contract import (
    DIRECTORY_PROTOCOL,
    NATIVE_DIRECTORY_PROTOCOL,
    decode_directory_request,
)
from aap_identity.directory_manager import directory_request_fingerprint
from session_helpers import ACTOR_ID, SECRET, audit_context, principal

CONTROL = "identity-directory-runtime-control-token"
DIRECTORY_SECRET = SECRET


def directory_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    token: str = "directory-test-bearer",
    payload: dict | None = None,
    audit: dict | None = None,
):
    payloads = {
        "list_users": {},
        "query_users": {},
        "user_stats": {},
        "export_users": {},
    }
    return decode_directory_request(
        {
            "schema": DIRECTORY_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/users"),
            "payload": payloads[operation] if payload is None else payload,
        }
    )


def directory_user(**changes):
    values = {
        "id": ACTOR_ID,
        "email": "person@example.test",
        "name": "Person",
        "role": "retired_global_role",
        "group_name": None,
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
        "preferences": {},
        "avatar_ref": None,
        "created_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    values.update(changes)
    return values


def directory_export_user(**changes):
    values = {
        "id": ACTOR_ID,
        "email": "person@example.test",
        "name": "Person",
        "role": "retired_global_role",
        "group_name": None,
        "group_id": None,
        "status": "historical_status",
        "created_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    values.update(changes)
    return values


def wire_success(request, data, *, current_principal=None):
    return {
        "schema": DIRECTORY_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": (current_principal or principal(role="super_admin")).model_dump(
            mode="json"
        ),
        "data": data,
    }


def native_command(
    request,
    *,
    actor=None,
    key_hash=None,
):
    from aap_identity.directory_contract import native_directory_command

    return native_directory_command(
        request,
        actor=actor or principal(role="super_admin"),
        expected_key_hash=key_hash,
        request_fingerprint=directory_request_fingerprint(DIRECTORY_SECRET, request),
        derivation_id="c" * 64,
    )


def native_success(command, data, *, replayed=False, current_principal=None):
    return {
        "schema": NATIVE_DIRECTORY_PROTOCOL,
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": (current_principal or command.actor).model_dump(mode="json"),
        "data": data,
        "replayed": replayed,
    }
