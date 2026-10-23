from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aap_identity.account_contract import AccountRecord
from aap_identity.group_contract import (
    GROUP_PROTOCOL,
    NATIVE_GROUP_PROTOCOL,
    GroupRecord,
    decode_group_request,
)
from account_helpers import account_record
from session_helpers import ACTOR_ID, audit_context, principal

SECRET = "identity-group-test-signing-secret"
CONTROL = "identity-group-runtime-control-token"


def group_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    token: str = "group-test-bearer",
    payload: dict | None = None,
    audit: dict | None = None,
):
    payloads = {
        "list_groups": {},
        "query_groups": {"page": 1, "page_size": 50, "search": None},
        "create_group": {"name": "New group", "description": None},
        "update_group": {
            "group_id": str(uuid.uuid4()),
            "name": None,
            "description": None,
        },
        "delete_group": {"group_id": str(uuid.uuid4())},
        "preview_group_assignments": {
            "user_ids": [str(ACTOR_ID)],
            "group_id": None,
        },
        "apply_group_assignments": {
            "user_ids": [str(ACTOR_ID)],
            "group_id": None,
        },
        "assign_user_group": {"user_id": str(uuid.uuid4()), "group_id": None},
    }
    return decode_group_request(
        {
            "schema": GROUP_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": audit or audit_context(path="/api/v1/groups"),
            "payload": payloads[operation] if payload is None else payload,
        }
    )


def group_record(**changes) -> GroupRecord:
    values = {
        "id": uuid.uuid4(),
        "name": "Example group",
        "description": None,
        "member_count": 0,
        "created_at": datetime(2026, 10, 20, tzinfo=UTC),
    }
    values.update(changes)
    return GroupRecord.model_validate(values)


def assigned_account(**changes) -> AccountRecord:
    values = {
        "id": uuid.uuid4(),
        "group_id": None,
        "group_name": None,
        "is_active": True,
    }
    values.update(changes)
    return account_record(**values)


def group_success(request, data, *, current_principal=None):
    return {
        "schema": GROUP_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": (current_principal or principal(role="super_admin")).model_dump(
            mode="json"
        ),
        "data": data,
    }


def native_success(command, data, *, current_principal=None):
    return {
        "schema": NATIVE_GROUP_PROTOCOL,
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": (current_principal or command.actor).model_dump(mode="json"),
        "data": data,
    }
