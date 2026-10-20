from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aap_identity.account_contract import AccountRecord, decode_account_request
from session_helpers import ACTOR_ID, audit_context

ACCOUNT_PROTOCOL = "aap-identity-accounts/v1"
NATIVE_ACCOUNT_PROTOCOL = "aap-identity-accounts-native/v1"


def account_record(**changes) -> AccountRecord:
    values = {
        "id": ACTOR_ID,
        "email": "person@example.test",
        "name": "Current account",
        "role": "employee",
        "group_name": None,
        "group_id": None,
        "status": "online",
        "is_active": True,
        "disabled_kind": None,
        "disabled_at": None,
        "disabled_by": None,
        "disabled_reason": None,
        "email_verified_at": None,
        "last_login_at": None,
        "last_seen_at": datetime(2035, 1, 1, tzinfo=UTC),
        "password_admin_reset_at": None,
        "deactivation_requested_at": None,
        "deactivation_scheduled_at": None,
        "preferences": {},
        "avatar_ref": None,
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    return AccountRecord.model_validate(values)


def account_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    token: str = "account-test-token",
    payload: dict | None = None,
    audit: dict | None = None,
):
    defaults = {
        "read_self_account": {},
        "get_preferences": {},
        "heartbeat": {},
        "update_name": {"name": "Updated name"},
        "patch_preferences": {"patch": {}},
        "change_password": {
            "old_password": "Current1Password",
            "new_password": "NewPassword!2026",
        },
        "set_avatar_ref": {
            "avatar_ref": "preset:blue",
            "kind": "preset_or_clear",
            "bytes": None,
        },
    }
    return decode_account_request(
        {
            "schema": ACCOUNT_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": audit or audit_context(),
            "payload": defaults[operation] if payload is None else payload,
        }
    )
