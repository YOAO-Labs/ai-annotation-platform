from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_v2_contract import (
    MAIL_V2_NATIVE_PROTOCOL,
    MAIL_V2_POLICY,
    MAIL_V2_POLICY_PROTOCOL,
    InvitationMailV2Lease,
    MailV2AcceptedReply,
    MailV2ClaimRequest,
    MailV2PolicyReply,
    RecoveryMailV2Lease,
    decode_mail_v2_accepted_reply,
    decode_mail_v2_claim_reply,
    decode_mail_v2_claim_request,
    decode_mail_v2_finish_request,
    decode_mail_v2_policy,
    decode_mail_v2_renew_request,
)

WORKER_ID = uuid.UUID("71000000-0000-4000-8000-000000000001")


def _common() -> dict:
    now = datetime.now(UTC)
    return {
        "id": "71000000-0000-4000-8000-000000000002",
        "email": "recipient@example.test",
        "frontend_base_url": "https://labels.example.test/app",
        "token_hash": "a" * 64,
        "token_ciphertext": "ciphertext-not-a-token",
        "encryption_id": "b" * 64,
        "expires_at": now.isoformat(),
        "attempt": 1,
        "worker_id": str(WORKER_ID),
        "lease_token": "opaque-lease-token",
        "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
    }


def _recovery_lease() -> dict:
    return {
        **_common(),
        "purpose": "password_reset",
        "actor_id": str(uuid.uuid4()),
    }


def _invitation_lease() -> dict:
    return {
        **_common(),
        "purpose": "invitation",
        "issuer_id": str(uuid.uuid4()),
        "invitation_id": str(uuid.uuid4()),
        "project_name": "Vision project",
        "project_member_role": "annotator",
    }


def _reply(lease: dict | None) -> dict:
    return {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"delivery": lease},
    }


def test_mail_v2_policy_is_exact_and_closed():
    result = decode_mail_v2_policy(
        {"schema": MAIL_V2_POLICY_PROTOCOL, "policy": MAIL_V2_POLICY}
    )
    assert isinstance(result, MailV2PolicyReply)
    for payload in (
        {"schema": MAIL_V2_POLICY_PROTOCOL, "policy": "wrong"},
        {
            "schema": MAIL_V2_POLICY_PROTOCOL,
            "policy": MAIL_V2_POLICY,
            "extra": True,
        },
    ):
        with pytest.raises(IdentityUnavailable):
            decode_mail_v2_policy(payload)


@pytest.mark.parametrize(
    ("wire", "model"),
    [
        (_recovery_lease(), RecoveryMailV2Lease),
        (_invitation_lease(), InvitationMailV2Lease),
    ],
)
def test_claim_reply_discriminates_recovery_and_invitation(wire, model):
    reply = decode_mail_v2_claim_reply(_reply(wire))
    assert isinstance(reply.data.delivery, model)
    assert "ciphertext-not-a-token" not in repr(reply)
    assert "opaque-lease-token" not in repr(reply)
    if model is InvitationMailV2Lease:
        assert reply.data.delivery.issuer_id == uuid.UUID(wire["issuer_id"])
        assert reply.data.delivery.invitation_id == uuid.UUID(wire["invitation_id"])
        assert not hasattr(reply.data.delivery, "actor_id")


def test_invitation_lease_rejects_recovery_actor_alias_and_unsafe_context():
    wire = _invitation_lease()
    with pytest.raises(IdentityUnavailable):
        decode_mail_v2_claim_reply(_reply({**wire, "actor_id": str(uuid.uuid4())}))
    with pytest.raises(IdentityUnavailable):
        decode_mail_v2_claim_reply(
            _reply({key: value for key, value in wire.items() if key != "project_name"})
        )
    with pytest.raises(IdentityUnavailable):
        decode_mail_v2_claim_reply(_reply({**wire, "project_member_role": "admin"}))
    with pytest.raises(IdentityUnavailable):
        decode_mail_v2_claim_reply(
            _reply({**wire, "project_name": "Project\nInjected"})
        )


def test_claim_request_is_closed_strict_and_rejects_duplicate_json_keys():
    payload = {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "worker_id": str(WORKER_ID),
        "lease_seconds": 60,
        "delivery_id": None,
    }
    assert decode_mail_v2_claim_request(payload).worker_id == WORKER_ID
    for changed in (
        {**payload, "lease_seconds": True},
        {**payload, "lease_seconds": 301},
        {**payload, "extra": 1},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_mail_v2_claim_request(changed)
    with pytest.raises(IdentityUnavailable):
        decode_mail_v2_claim_request(
            (
                '{"schema":"aap-identity-mail-native/v2",'
                f'"worker_id":"{WORKER_ID}","lease_seconds":60,'
                '"delivery_id":null,"lease_seconds":60}'
            )
        )


def test_renew_and_finish_requests_hide_lease_and_restrict_safe_outcomes():
    request = {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "delivery_id": str(uuid.uuid4()),
        "worker_id": str(WORKER_ID),
        "lease_token": "opaque-lease-token",
    }
    assert decode_mail_v2_renew_request(request).worker_id == WORKER_ID
    base = {
        **request,
        "outcome": "delivered",
        "reason": "sent",
        "retry_delay_seconds": None,
    }
    assert decode_mail_v2_finish_request(base).outcome == "delivered"
    assert (
        decode_mail_v2_finish_request(
            {
                **base,
                "outcome": "unknown",
                "reason": "transport_failed",
                "retry_delay_seconds": 30,
            }
        ).retry_delay_seconds
        == 30
    )
    for changed in (
        {**base, "outcome": "delivered", "reason": "transport_failed"},
        {**base, "outcome": "unknown", "reason": "transport_failed"},
        {**base, "outcome": "retry", "reason": "sent", "retry_delay_seconds": 30},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_mail_v2_finish_request(changed)
    assert "opaque-lease-token" not in repr(decode_mail_v2_renew_request(request))


def test_accepted_reply_requires_boolean_and_closed_envelope():
    payload = {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"accepted": False},
    }
    assert isinstance(decode_mail_v2_accepted_reply(payload), MailV2AcceptedReply)
    for invalid in (
        {**payload, "ok": 1},
        {**payload, "data": {"accepted": 1}},
        {**payload, "data": {"accepted": False, "extra": True}},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_mail_v2_accepted_reply(invalid)
    with pytest.raises(ValidationError):
        MailV2ClaimRequest.model_validate(
            {
                "schema": MAIL_V2_NATIVE_PROTOCOL,
                "worker_id": str(WORKER_ID),
                "lease_seconds": True,
                "delivery_id": None,
            }
        )
