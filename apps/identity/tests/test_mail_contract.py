from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_contract import (
    MAIL_NATIVE_PROTOCOL,
    MailAcceptedReply,
    MailClaimReply,
    decode_mail_accepted_reply,
    decode_mail_claim_reply,
    decode_mail_claim_request,
    decode_mail_delivery_policy,
    decode_mail_finish_request,
)
from pydantic import ValidationError


def test_mail_claim_request_is_closed_strict_and_requires_nullable_delivery_id():
    value = decode_mail_claim_request(
        {
            "schema": MAIL_NATIVE_PROTOCOL,
            "worker_id": str(uuid.uuid4()),
            "lease_seconds": 60,
            "delivery_id": None,
        }
    )
    assert value.delivery_id is None

    for change in (
        {"unexpected": True},
        {"protocol": MAIL_NATIVE_PROTOCOL},
        {"lease_seconds": True},
        {"lease_seconds": 301},
    ):
        request = {
            "schema": MAIL_NATIVE_PROTOCOL,
            "worker_id": str(uuid.uuid4()),
            "lease_seconds": 60,
            "delivery_id": None,
            **change,
        }
        with pytest.raises(IdentityUnavailable):
            decode_mail_claim_request(request)


def test_mail_reply_rejects_extra_fields_and_non_boolean_acceptance():
    with pytest.raises(IdentityUnavailable):
        decode_mail_accepted_reply(
            {"schema": MAIL_NATIVE_PROTOCOL, "ok": True, "data": {"accepted": 1}}
        )
    with pytest.raises(IdentityUnavailable):
        decode_mail_accepted_reply(
            {
                "schema": MAIL_NATIVE_PROTOCOL,
                "ok": True,
                "data": {"accepted": True, "detail": "unsafe"},
            }
        )

    claim = {
        "schema": MAIL_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"delivery": None},
    }
    accepted = {
        "schema": MAIL_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"accepted": True},
    }
    for decoder, reply in (
        (decode_mail_claim_reply, claim),
        (decode_mail_accepted_reply, accepted),
    ):
        with pytest.raises(IdentityUnavailable):
            decoder({**reply, "ok": 1})

    with pytest.raises(ValidationError):
        MailClaimReply.model_validate({**claim, "ok": 1})
    with pytest.raises(ValidationError):
        MailAcceptedReply.model_validate({**accepted, "ok": 1})


def test_delivery_policy_hides_smtp_password_from_repr_and_rejects_extra_fields():
    policy = decode_mail_delivery_policy(
        {
            "smtp_host": "smtp.example.test",
            "smtp_port": 587,
            "smtp_user": "mail-user",
            "smtp_password": "smtp-secret-value",
            "smtp_from": "noreply@example.test",
        }
    )
    assert "smtp-secret-value" not in repr(policy)
    assert "mail-user" not in repr(policy)
    with pytest.raises(IdentityUnavailable):
        decode_mail_delivery_policy(
            {
                **policy.model_dump(),
                "unexpected": "value",
            }
        )


def test_delivery_policy_preserves_long_settings_strings():
    policy = decode_mail_delivery_policy(
        {
            "smtp_host": "h" * 300,
            "smtp_port": 587,
            "smtp_user": "u" * 300,
            "smtp_password": "p" * 5000,
            "smtp_from": "noreply@" + "x" * 400,
        }
    )
    assert len(policy.smtp_host) == 300
    assert len(policy.smtp_user) == 300
    assert len(policy.smtp_password) == 5000
    assert len(policy.smtp_from) == 408


def test_retry_finish_requires_bounded_retry_and_closed_safe_reason():
    command = {
        "schema": MAIL_NATIVE_PROTOCOL,
        "delivery_id": str(uuid.uuid4()),
        "worker_id": str(uuid.uuid4()),
        "lease_token": "lease-secret",
        "outcome": "unknown",
        "reason": "transport_failed",
        "retry_delay_seconds": 30,
    }
    parsed = decode_mail_finish_request(command)
    assert parsed.retry_delay_seconds == 30
    assert "lease-secret" not in repr(parsed)

    for change in (
        {"retry_delay_seconds": None},
        {"retry_delay_seconds": 301},
        {"reason": "exception: SMTP password leaked"},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_mail_finish_request({**command, **change})


def test_mail_lease_requires_aware_timestamps_and_hides_link_secrets():
    value = {
        "id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
        "purpose": "password_reset",
        "email": "user@example.test",
        "frontend_base_url": "https://example.test",
        "token_hash": "a" * 64,
        "token_ciphertext": "ciphertext-secret",
        "encryption_id": "b" * 64,
        "expires_at": datetime.now(UTC).isoformat(),
        "attempt": 2,
        "worker_id": str(uuid.uuid4()),
        "lease_token": "lease-secret",
        "lease_expires_at": datetime.now(UTC).isoformat(),
    }
    from aap_identity.mail_contract import MailLease

    lease = MailLease.model_validate_json(__import__("json").dumps(value))
    assert "ciphertext-secret" not in repr(lease)
    assert "lease-secret" not in repr(lease)
    assert "https://example.test" not in repr(lease)

    value["expires_at"] = "2030-01-01T00:00:00"
    with pytest.raises(Exception):
        MailLease.model_validate_json(__import__("json").dumps(value))
