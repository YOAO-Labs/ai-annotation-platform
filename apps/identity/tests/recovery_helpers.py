from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from aap_identity.account_contract import AccountRecord
from aap_identity.recovery_contract import (
    NATIVE_RECOVERY_PROTOCOL,
    RECOVERY_PROTOCOL,
    NativeRecoveryApplyReply,
    NativeRecoveryPrepareReply,
    RecoveryActor,
    RecoveryPolicy,
    RecoveryTokenRecord,
    decode_recovery_request,
)
from aap_identity.recovery_crypto import mail_key_from_secret
from aap_identity.session_contract import SessionClaims
from account_helpers import account_record
from session_helpers import ACTOR_ID, audit_context

SECRET = "identity-recovery-test-signing-secret"
MAIL_KEY = mail_key_from_secret(SECRET)
CONTROL = "identity-recovery-control-token"


def recovery_request(operation: str, *, payload=None, operation_id=None):
    payloads = {
        "register_open": {
            "email": "person@example.test",
            "name": "New account",
            "password": "StrongPassword2026",
            "captcha_verified": True,
        },
        "request_password_reset": {
            "email": "person@example.test",
            "captcha_verified": True,
        },
        "resend_email_verification": {"email": "person@example.test"},
        "consume_password_reset": {
            "token": "legacy-token-123",
            "new_password": "StrongPassword2026",
        },
        "consume_email_verification": {"token": "legacy-token-123"},
    }
    return decode_recovery_request(
        {
            "schema": RECOVERY_PROTOCOL,
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "audit_context": audit_context(path="/api/v1/auth/" + operation),
            "payload": payloads[operation] if payload is None else payload,
        }
    )


def actor(
    *,
    actor_id=ACTOR_ID,
    email="person@example.test",
    role="employee",
    active=True,
    verified_at=None,
):
    return RecoveryActor(
        id=actor_id,
        email=email,
        role=role,
        is_active=active,
        credential_generation=0,
        email_verified_at=verified_at,
    )


def account(
    *,
    actor_id=ACTOR_ID,
    email="person@example.test",
    role="viewer",
    status=None,
    verified_at=None,
) -> AccountRecord:
    return account_record(
        id=actor_id,
        email=email,
        role=role,
        status=status or ("online" if verified_at is not None else "offline"),
        email_verified_at=verified_at,
    )


def claims(operation_id, *, actor_id=ACTOR_ID, role="viewer", gen=0):
    return SessionClaims(
        sub=actor_id,
        role=role,
        exp=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        jti=operation_id,
        gen=gen,
        iss="aap-identity/v1",
    )


def policy(*, verify=True):
    return RecoveryPolicy(
        allow_open_registration=True,
        email_verification_required=verify,
        frontend_base_url="https://anno.example.test",
        expire_minutes=1440,
        password_reset_ttl_seconds=3600,
        email_verification_ttl_seconds=86400,
    )


def token_record(token_hash: str, *, email="person@example.test"):
    return RecoveryTokenRecord(
        id=uuid.uuid4(),
        token_hash=token_hash,
        issued_email=email,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        used_at=None,
    )


def prepare_reply(
    command,
    *,
    actor_value=None,
    token=None,
    eligible=False,
    replay=None,
    password_hash=None,
    resolved_policy=None,
):
    return NativeRecoveryPrepareReply(
        schema=NATIVE_RECOVERY_PROTOCOL,
        ok=True,
        operation=command.operation,
        operation_id=command.operation_id,
        data={
            "actor": actor_value,
            "password_hash": password_hash,
            "token": token,
            "eligible": eligible,
            "replay": replay,
            "policy": resolved_policy or policy(),
        },
    )


def apply_reply(
    command,
    result,
    *,
    actor_value=None,
    replayed=False,
    password_changed=False,
    email_verified_changed=False,
):
    return NativeRecoveryApplyReply(
        schema=NATIVE_RECOVERY_PROTOCOL,
        ok=True,
        operation=command.operation,
        operation_id=command.operation_id,
        data={
            "result": result,
            "actor": actor_value,
            "replayed": replayed,
            "password_changed": password_changed,
            "email_verified_changed": email_verified_changed,
        },
    )
