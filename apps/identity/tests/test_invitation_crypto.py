from __future__ import annotations

import uuid

import pytest

from aap_identity.invitation_crypto import (
    decrypt_mail_token,
    encrypt_mail_token,
    invitation_id,
    invitation_request_fingerprint,
    invitation_token,
    mail_key_from_secret,
    token_hash,
)
from invitation_helpers import INVITATION_SECRET, invitation_request


def test_invitation_token_and_id_are_stable_and_operation_scoped():
    operation_id = uuid.uuid4()
    created = invitation_token(
        INVITATION_SECRET, operation_id, operation="create_invitation"
    )
    assert len(created) == 43
    assert (
        invitation_token(INVITATION_SECRET, operation_id, operation="create_invitation")
        == created
    )
    assert (
        invitation_token(INVITATION_SECRET, operation_id, operation="resend_invitation")
        != created
    )
    first_id = invitation_id(INVITATION_SECRET, operation_id, item_index=0)
    assert invitation_id(INVITATION_SECRET, operation_id, item_index=0) == first_id
    assert invitation_id(INVITATION_SECRET, operation_id, item_index=1) != first_id


def test_invitation_fingerprint_excludes_bearer_and_audit_but_binds_payload():
    operation_id = uuid.uuid4()
    first = invitation_request(
        "create_invitation",
        operation_id=operation_id,
        bearer="first-private-bearer",
        audit={"method": "POST", "path": "/first", "ip": None, "request_id": None},
    )
    retry = invitation_request(
        "create_invitation",
        operation_id=operation_id,
        bearer="rotated-private-bearer",
        audit={"method": "POST", "path": "/retry", "ip": None, "request_id": None},
    )
    changed = invitation_request(
        "create_invitation",
        operation_id=operation_id,
        payload={
            "email": "different@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": None,
            "project_member_role": None,
        },
    )
    assert invitation_request_fingerprint(INVITATION_SECRET, first) == (
        invitation_request_fingerprint(INVITATION_SECRET, retry)
    )
    assert invitation_request_fingerprint(INVITATION_SECRET, first) != (
        invitation_request_fingerprint(INVITATION_SECRET, changed)
    )


@pytest.mark.parametrize("token", ["é", "邀请-token", "x" * 64])
def test_mail_encryption_round_trips_opaque_unicode_invitation_tokens(token):
    key = mail_key_from_secret(INVITATION_SECRET)
    digest, ciphertext, epoch = encrypt_mail_token(token, key)
    assert digest == token_hash(token)
    assert decrypt_mail_token(ciphertext, key, epoch) == token


def test_mail_decryption_rejects_key_epoch_mismatch():
    token = "opaque/邀请-token"
    digest, ciphertext, epoch = encrypt_mail_token(
        token, mail_key_from_secret(INVITATION_SECRET)
    )
    assert digest == token_hash(token)
    wrong_key = mail_key_from_secret("different-invitation-secret")
    with pytest.raises(ValueError):
        decrypt_mail_token(ciphertext, wrong_key, epoch)


def test_invitation_crypto_rejects_invalid_token_lengths():
    key = mail_key_from_secret(INVITATION_SECRET)
    with pytest.raises(ValueError):
        encrypt_mail_token("", key)
    with pytest.raises(ValueError):
        encrypt_mail_token("x" * 65, key)
