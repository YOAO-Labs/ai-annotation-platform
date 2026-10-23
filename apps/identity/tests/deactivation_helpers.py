from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from aap_identity.account_contract import AccountRecord
from aap_identity.deactivation_contract import (
    DEACTIVATION_PROTOCOL,
    DUE_PROTOCOL,
    NATIVE_DEACTIVATION_PROTOCOL,
    NATIVE_DUE_PROTOCOL,
    decode_deactivation_due_request,
    decode_deactivation_request,
)
from account_helpers import account_record
from session_helpers import audit_context, principal

SECRET = "identity-deactivation-test-signing-secret"
CONTROL = "identity-deactivation-runtime-control-token"
ACTOR_ID = uuid.UUID("5c5633f5-0460-4c22-89ba-783af26c2cda")


def deactivation_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    token: str = "deactivation-test-bearer",
    payload: dict | None = None,
    audit: dict | None = None,
):
    defaults = {
        "request_self_deactivation": {"reason": None},
        "cancel_self_deactivation": {},
    }
    return decode_deactivation_request(
        {
            "schema": DEACTIVATION_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": audit
            or audit_context(path="/api/v1/auth/me/deactivation"),
            "payload": defaults[operation] if payload is None else payload,
        }
    )


def due_request(*, operation_id=None, limit=256):
    return decode_deactivation_due_request(
        {
            "schema": DUE_PROTOCOL,
            "operation_id": str(operation_id or uuid.uuid4()),
            "limit": limit,
        }
    )


def account(
    *,
    actor_id=ACTOR_ID,
    requested_at=None,
    scheduled_at=None,
    **changes,
) -> AccountRecord:
    values = {
        "id": actor_id,
        "is_active": True,
        "deactivation_requested_at": requested_at,
        "deactivation_scheduled_at": scheduled_at,
    }
    values.update(changes)
    return account_record(**values)


def requested_account(*, actor_id=ACTOR_ID, role="employee") -> AccountRecord:
    requested = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    return account(
        actor_id=actor_id,
        requested_at=requested,
        scheduled_at=requested + timedelta(days=7),
        role=role,
    )


def user_success(request, account_value, *, current_principal=None):
    return {
        "schema": DEACTIVATION_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": (current_principal or principal(role="employee")).model_dump(
            mode="json"
        ),
        "data": {"account": account_value.model_dump(mode="json")},
    }


def native_user_success(command, account_value, *, current_principal=None):
    return {
        "schema": NATIVE_DEACTIVATION_PROTOCOL,
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": (current_principal or command.actor).model_dump(mode="json"),
        "data": {"account": account_value.model_dump(mode="json")},
    }


def due_success(request, **changes):
    data = {
        "schema": DUE_PROTOCOL,
        "ok": True,
        "operation_id": str(request.operation_id),
        "processed": 0,
        "examined": 0,
        "blocked_last_super_admin": 0,
        "has_more": False,
    }
    data.update(changes)
    return data


def native_due_success(request, **changes):
    data = {
        "schema": NATIVE_DUE_PROTOCOL,
        "ok": True,
        "operation_id": str(request.operation_id),
        "processed": 0,
        "examined": 0,
        "blocked_last_super_admin": 0,
        "has_more": False,
    }
    data.update(changes)
    return data
