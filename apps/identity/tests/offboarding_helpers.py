from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from aap_identity.directory_contract import DirectoryUserRecord
from aap_identity.offboarding_contract import (
    NATIVE_OFFBOARDING_PROTOCOL,
    OFFBOARDING_PROTOCOL,
    decode_offboarding_request,
)
from aap_identity.offboarding_manager import (
    offboarding_derivation_id,
    offboarding_request_fingerprint,
)
from session_helpers import audit_context, principal

OFFBOARDING_SECRET = "identity-offboarding-test-secret"
OFFBOARDING_CONTROL = "identity-offboarding-control-token"
TARGET_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OWNER_RECEIVER_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ANNOTATOR_RECEIVER_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
REVIEWER_RECEIVER_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
BATCH_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TASK_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
COMPLETED_TASK_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
LOCK_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
COMPLETED_LOCK_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
KEY_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
OPERATION_ID = uuid.UUID("fedcba98-7654-4321-8fed-cba987654321")
TASK_TIME = datetime(2026, 1, 2, 3, 4, 5, 246912, tzinfo=UTC)
CREATED_AT = datetime(2025, 10, 1, tzinfo=UTC)


def offboarding_request(
    operation: str = "preview_offboarding",
    *,
    operation_id: uuid.UUID | None = None,
    token: str = "private-offboarding-bearer",
    target_user_id: uuid.UUID = TARGET_ID,
    preview_version: str = "0" * 64,
    reason: str = "  End of assignment  ",
    mode: str = "handoff",
    projects: list[dict] | None = None,
    audit: dict | None = None,
):
    if operation == "preview_offboarding":
        payload = {"target_user_id": str(target_user_id)}
    else:
        payload = {
            "target_user_id": str(target_user_id),
            "preview_version": preview_version,
            "reason": reason,
            "mode": mode,
            "projects": projects
            if projects is not None
            else [
                {
                    "project_id": str(PROJECT_ID),
                    "owner_receiver_id": str(OWNER_RECEIVER_ID),
                    "annotator_receiver_id": str(ANNOTATOR_RECEIVER_ID),
                    "reviewer_receiver_id": str(REVIEWER_RECEIVER_ID),
                }
            ],
        }
    return decode_offboarding_request(
        {
            "schema": OFFBOARDING_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or OPERATION_ID),
            "token": token,
            "audit_context": audit
            or audit_context(path="/api/v1/users/target/offboarding"),
            "payload": payload,
        }
    )


def offboarding_user(**changes) -> DirectoryUserRecord:
    value = {
        "id": TARGET_ID,
        "email": "target@example.test",
        "name": "Target user",
        "role": "employee",
        "group_name": None,
        "group_id": None,
        "status": "online",
        "is_active": True,
        "disabled_kind": None,
        "disabled_at": None,
        "disabled_by": None,
        "disabled_reason": None,
        "email_verified_at": TASK_TIME,
        "last_login_at": TASK_TIME,
        "last_seen_at": TASK_TIME,
        "password_admin_reset_at": None,
        "deactivation_requested_at": None,
        "deactivation_scheduled_at": None,
        "preferences": {"opaque": {"number": 9007199254740993}},
        "avatar_ref": None,
        "created_at": CREATED_AT,
    }
    value.update(changes)
    return DirectoryUserRecord.model_validate_json(json.dumps(value, default=str))


def offboarding_facts(
    *,
    target: DirectoryUserRecord | None = None,
    other_unreserved: int = 2,
):
    from aap_identity.offboarding_contract import OffboardingSnapshotFacts

    values = {
        "target": (target or offboarding_user()).model_dump(mode="json"),
        "projects": [
            {
                "id": str(PROJECT_ID),
                "name": "Research",
                "owner_id": str(TARGET_ID),
            }
        ],
        "members": [
            {
                "id": "77777777-7777-4777-8777-777777777777",
                "project_id": str(PROJECT_ID),
                "role": "annotator",
            },
        ],
        "batches": [
            {
                "id": str(BATCH_ID),
                "project_id": str(PROJECT_ID),
                "name": "Batch 1",
                "status": "active",
                "annotator_id": str(TARGET_ID),
                "reviewer_id": None,
                "assigned_user_ids": [str(TARGET_ID)],
            }
        ],
        "tasks": [
            {
                "id": str(TASK_ID),
                "project_id": str(PROJECT_ID),
                "batch_id": str(BATCH_ID),
                "status": "in_progress",
                "assignee_id": str(TARGET_ID),
                "reviewer_id": str(TARGET_ID),
                "version": 4,
            },
            {
                "id": str(COMPLETED_TASK_ID),
                "project_id": str(PROJECT_ID),
                "batch_id": str(BATCH_ID),
                "status": "completed",
                "assignee_id": str(TARGET_ID),
                "reviewer_id": None,
                "version": 5,
            },
        ],
        "locks": [
            {
                "id": str(LOCK_ID),
                "task_id": str(TASK_ID),
                "expire_at": TASK_TIME.isoformat(),
            },
            {
                "id": str(COMPLETED_LOCK_ID),
                "task_id": str(COMPLETED_TASK_ID),
                "expire_at": TASK_TIME.isoformat(),
            },
        ],
        "api_keys": [
            {
                "id": str(KEY_ID),
                "name": "Active key",
                "key_prefix": "ak_test",
                "last_used_at": TASK_TIME.isoformat(),
                "revoked_at": None,
                "created_at": CREATED_AT.isoformat(),
            }
        ],
        "candidate_users": [
            {
                "id": str(OWNER_RECEIVER_ID),
                "name": "Ada Owner",
                "email": "ada@example.test",
                "role": "project_admin",
            },
            {
                "id": str(ANNOTATOR_RECEIVER_ID),
                "name": "Ben Annotator",
                "email": "ben@example.test",
                "role": "employee",
            },
            {
                "id": str(REVIEWER_RECEIVER_ID),
                "name": "Cora Reviewer",
                "email": "cora@example.test",
                "role": "employee",
            },
        ],
        "candidate_memberships": [
            {
                "project_id": str(PROJECT_ID),
                "user_id": str(ANNOTATOR_RECEIVER_ID),
                "role": "annotator",
            },
            {
                "project_id": str(PROJECT_ID),
                "user_id": str(REVIEWER_RECEIVER_ID),
                "role": "reviewer",
            },
        ],
        "other_unreserved": other_unreserved,
    }
    return OffboardingSnapshotFacts.model_validate_json(
        json.dumps(values, ensure_ascii=True, allow_nan=False)
    )


def native_prepare_success(command, facts, *, current_principal=None):
    return json.loads(
        json.dumps(
            {
                "schema": NATIVE_OFFBOARDING_PROTOCOL,
                "phase": "prepare",
                "ok": True,
                "operation": command.operation,
                "operation_id": str(command.operation_id),
                "principal": (current_principal or command.actor).model_dump(
                    mode="json"
                ),
                "data": {
                    "kind": "snapshot",
                    "transaction_id": "tx-offboarding-1",
                    "target_generation": 7,
                    "target_is_active": facts.target.is_active,
                    "facts": facts.model_dump(mode="json"),
                },
            },
            ensure_ascii=True,
        )
    )


def native_apply_success(
    command,
    *,
    current_principal=None,
    audit_id: int = 73,
    user_changes: dict | None = None,
    result_user: DirectoryUserRecord | None = None,
    replayed: bool = False,
):
    from aap_identity.offboarding_contract import OffboardingResultRecord

    payload = command.payload
    if result_user is None:
        result_user = offboarding_user(
            status="offline",
            is_active=False,
            disabled_kind=(
                "emergency_suspended"
                if payload.mode == "emergency_suspend"
                else "suspended"
            ),
            disabled_at=TASK_TIME,
            disabled_by=command.actor.actor_id,
            disabled_reason=payload.normalized_reason,
            **(user_changes or {}),
        )
    result = OffboardingResultRecord(
        user=result_user,
        status="suspended",
        mode=payload.mode,
        transfers=payload.transfers,
        unresolved=payload.unresolved,
        revoked_api_key_ids=[KEY_ID],
        audit_id=audit_id,
    )
    return {
        "schema": NATIVE_OFFBOARDING_PROTOCOL,
        "phase": "apply",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": (current_principal or command.actor).model_dump(mode="json"),
        "data": {"result": result.model_dump(mode="json")},
        "replayed": replayed,
    }


def native_command_for_test(request, actor=None, key_hash=None):
    from aap_identity.offboarding_contract import native_prepare_offboarding_command

    actor = actor or principal(role="super_admin")
    return native_prepare_offboarding_command(
        request,
        actor=actor,
        expected_key_hash=key_hash,
        request_fingerprint=offboarding_request_fingerprint(
            OFFBOARDING_SECRET, request
        ),
        derivation_id=offboarding_derivation_id(OFFBOARDING_SECRET),
    )
