from __future__ import annotations

import uuid
import json
from datetime import UTC, datetime

from aap_identity.admin_password_contract import (
    ADMIN_PASSWORD_PROTOCOL,
    NATIVE_ADMIN_PASSWORD_PROTOCOL,
    decode_admin_password_request,
)
from session_helpers import audit_context, principal

CONTROL = "identity-admin-password-runtime-control-token"
SECRET = "identity-admin-password-test-signing-secret"
TARGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION_ID = uuid.UUID("fedcba98-7654-4321-8fed-cba987654321")
PASSWORD_HASH = "$2b$12$" + ("." * 53)


def admin_password_request(
    *,
    operation_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID = TARGET_ID,
    token: str = "admin-password-test-bearer",
    audit: dict | None = None,
):
    return decode_admin_password_request(
        {
            "schema": ADMIN_PASSWORD_PROTOCOL,
            "operation": "admin_reset_password",
            "operation_id": str(operation_id or OPERATION_ID),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/users/admin-reset"),
            "payload": {"target_user_id": str(target_user_id)},
        }
    )


def admin_password_data(**changes):
    values = {
        "target_user_id": TARGET_ID,
        "target_email": "target@example.test",
        "temp_password": "GLwLvE4ri9v0V!#y",
        "password_admin_reset_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    values.update(changes)
    return values


def native_admin_password_command(request, *, actor=None, key_hash=None, new_hash=None):
    from aap_identity.admin_password_manager import (
        admin_password_derivation_id,
        admin_password_request_fingerprint,
    )
    from aap_identity.admin_password_contract import native_admin_password_command

    return native_admin_password_command(
        request,
        actor=actor or principal(role="super_admin"),
        expected_key_hash=key_hash,
        request_fingerprint=admin_password_request_fingerprint(SECRET, request),
        derivation_id=admin_password_derivation_id(SECRET),
        new_password_hash=new_hash or PASSWORD_HASH,
    )


def native_admin_password_success(
    command, data, *, replayed=False, current_principal=None
):
    return json.loads(
        json.dumps(
            {
                "schema": NATIVE_ADMIN_PASSWORD_PROTOCOL,
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


def wire_admin_password_success(request, data, *, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": ADMIN_PASSWORD_PROTOCOL,
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
