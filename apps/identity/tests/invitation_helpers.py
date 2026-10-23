from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from aap_identity.account_contract import AccountRecord
from aap_identity.contract import Principal
from aap_identity.invitation_contract import (
    INVITATION_PROTOCOL,
    InvitationNativeResult,
    InvitationPolicy,
    InvitationRecord,
    InvitationRequest,
    NativeInvitationActor,
    NativeInvitationApplyReply,
    NativeInvitationPrepareReply,
    decode_invitation_request,
)
from aap_identity.recovery_crypto import mail_key_from_secret
from account_helpers import account_record
from session_helpers import audit_context, principal as session_principal

INVITATION_SECRET = "invitation-owner-test-secret-2026"
MAIL_KEY = mail_key_from_secret(INVITATION_SECRET)
CONTROL = "invitation-runtime-control-token"
INVITATION_ID = uuid.UUID("77570ae1-e6c1-4b45-8c60-f13f1a39cc46")
INVITER_ID = uuid.UUID("3420fd45-205d-43a8-b08c-cf4608b12ad0")
PROJECT_ID = uuid.UUID("ac2f005c-49b7-4a47-8ff5-6eeb574bd24c")
EMAIL = "invitee@example.test"


def principal(*, actor_id=INVITER_ID, role="super_admin", **changes) -> Principal:
    return session_principal(actor_id=actor_id, role=role, **changes)


def actor_record(
    *,
    actor_id=INVITER_ID,
    email="inviter@example.test",
    name="Inviter",
    role="super_admin",
    is_active=True,
    generation=7,
) -> NativeInvitationActor:
    return NativeInvitationActor(
        id=actor_id,
        email=email,
        name=name,
        role=role,
        is_active=is_active,
        credential_generation=generation,
    )


def invitation_record(**changes) -> InvitationRecord:
    values = {
        "id": INVITATION_ID,
        "email": EMAIL,
        "role": "employee",
        "group_name": None,
        "project_id": None,
        "project_name": None,
        "project_member_role": None,
        "status": "pending",
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "invited_by": INVITER_ID,
        "invited_by_name": "Inviter",
        "accepted_at": None,
        "revoked_at": None,
        "created_at": datetime.now(UTC),
    }
    values.update(changes)
    return InvitationRecord.model_validate(values)


def invitation_policy(**changes) -> InvitationPolicy:
    values = {
        "max_invitations_per_day": 30,
        "invitation_ttl_days": 7,
        "frontend_base_url": "https://anno.example.test",
        "expire_minutes": 1440,
    }
    values.update(changes)
    return InvitationPolicy.model_validate(values)


def invitation_user(
    *,
    user_id: uuid.UUID,
    email=EMAIL,
    name="New Invitee",
    role="employee",
    status="online",
    email_verified_at=None,
) -> AccountRecord:
    return account_record(
        id=user_id,
        email=email,
        name=name,
        role=role,
        status=status,
        is_active=True,
        email_verified_at=email_verified_at or datetime.now(UTC),
    )


def invitation_acceptance(*, project_id=None, project_name=None, role=None):
    return {
        "project_id": str(project_id) if project_id else None,
        "project_name": project_name,
        "project_member_role": role,
        "next_action": "wait_for_allocation",
        "next_action_label": "等待管理员分配项目",
        "responsible_person_name": "Owner" if project_id else None,
        "active_batch_count": 0,
    }


def invitation_request(
    operation: str,
    *,
    operation_id: uuid.UUID | None = None,
    bearer: str | None = None,
    payload: dict | None = None,
    audit: dict | None = None,
) -> InvitationRequest:
    defaults = {
        "create_invitation": {
            "email": "invitee@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": None,
            "project_member_role": None,
        },
        "preview_invitation_batch": {
            "items": [
                {
                    "email": "batch@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                }
            ]
        },
        "apply_invitation_batch": {
            "items": [
                {
                    "email": "batch@example.test",
                    "role": "employee",
                    "group_name": None,
                    "project_id": None,
                    "project_member_role": None,
                }
            ]
        },
        "list_invitations": {"status": "all", "scope": "me"},
        "query_invitations": {
            "page": 1,
            "page_size": 50,
            "status": "all",
            "scope": "me",
        },
        "invitation_stats": {"status": "all", "scope": "me"},
        "export_invitations": {"format": "csv", "status": "all", "scope": "me"},
        "revoke_invitation": {"invitation_id": str(INVITATION_ID)},
        "resend_invitation": {"invitation_id": str(INVITATION_ID)},
        "send_invitation_email": {"invitation_id": str(INVITATION_ID)},
        "resolve_invitation": {"invitation_token": "opaque-invite-token"},
        "register_invitation": {
            "invitation_token": "opaque-invite-token",
            "name": "New Invitee",
            "password": "StrongPassword!2026",
        },
        "accept_invitation": {"invitation_token": "opaque-invite-token"},
    }
    if payload is None:
        payload = defaults[operation]
    if operation in {"resolve_invitation", "register_invitation"}:
        bearer = None
    elif bearer is None:
        bearer = "invitation-test-bearer"
    return decode_invitation_request(
        {
            "schema": INVITATION_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": bearer,
            "audit_context": audit or audit_context(path=f"/api/v1/{operation}"),
            "payload": payload,
        }
    )


def result(**changes) -> InvitationNativeResult:
    values = {
        "invitation": None,
        "invitations": None,
        "items": None,
        "total": None,
        "page": None,
        "page_size": None,
        "pages": None,
        "stats": None,
        "user": None,
        "claims": None,
        "acceptance": None,
        "email_verification_required": None,
        "preview": None,
        "succeeded": None,
        "failed": None,
        "exported_by": None,
        "exported_at": None,
        "invitation_id": None,
        "email": None,
        "mail_status": None,
        "mail_failure_code": None,
        "frontend_base_url": None,
    }
    values.update(changes)
    return InvitationNativeResult.model_validate(values)


def prepare_reply(
    command,
    *,
    actor_value=None,
    policy=None,
    invitation=None,
    project=None,
    target=None,
    membership_exists=None,
    existing_password_hash=None,
    invitation_token=None,
    mail_token_hash=None,
    mail_token_ciphertext=None,
    mail_encryption_id=None,
    mail_frontend_url=None,
    replayed=False,
    native_result=None,
):
    return NativeInvitationPrepareReply(
        schema="aap-identity-invitations-native/v1",
        ok=True,
        operation=command.operation,
        operation_id=command.operation_id,
        principal=command.actor,
        data={
            "policy": policy,
            "actor": actor_value,
            "invitation": invitation,
            "project": project,
            "target": target,
            "membership_exists": membership_exists,
            "existing_password_hash": existing_password_hash,
            "invitation_token": invitation_token,
            "mail_token_hash": mail_token_hash,
            "mail_token_ciphertext": mail_token_ciphertext,
            "mail_encryption_id": mail_encryption_id,
            "mail_frontend_url": mail_frontend_url,
            "replayed": replayed,
            "result": native_result,
        },
    )


def apply_reply(command, native_result: InvitationNativeResult, *, replayed=False):
    return NativeInvitationApplyReply(
        schema="aap-identity-invitations-native/v1",
        ok=True,
        operation=command.operation,
        operation_id=command.operation_id,
        principal=command.actor,
        data={
            "actor": (
                actor_record(
                    actor_id=command.actor.actor_id,
                    email=command.actor.email,
                    name=command.actor.name,
                    role=command.actor.role,
                    generation=command.actor.generation or 0,
                )
                if command.actor is not None
                else None
            ),
            "replayed": replayed,
            "result": native_result,
        },
    )
