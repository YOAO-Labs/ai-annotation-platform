from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from aap_identity.recovery_crypto import (
    RECOVERY_DERIVATION,
    RECOVERY_MAIL_DERIVATION,
    decrypt_mail_token,
    mail_encryption_id,
    mail_key_from_secret,
    request_fingerprint,
    token_hash,
)
from cryptography.fernet import Fernet
from recovery_helpers import recovery_request

VECTOR = json.loads(
    (Path(__file__).parent / "fixtures" / "recovery_vectors.json").read_text()
)


def test_hash_and_request_fingerprint_have_stable_keyed_vectors():
    assert token_hash(VECTOR["token"]) == VECTOR["token_hash"]
    request = recovery_request(
        VECTOR["request"]["operation"], payload=VECTOR["request"]["payload"]
    )
    master = hmac.new(
        VECTOR["secret"].encode(),
        RECOVERY_DERIVATION.encode("ascii"),
        hashlib.sha256,
    ).digest()
    fingerprint, epoch = request_fingerprint(VECTOR["secret"], request)
    assert epoch == VECTOR["derivation_id"] == hashlib.sha256(master).hexdigest()
    assert fingerprint == VECTOR["request_fingerprint"]
    assert request_fingerprint(VECTOR["secret"], request) == (fingerprint, epoch)
    assert request_fingerprint("rotated-secret", request)[0] != fingerprint


def test_fallback_mail_key_is_domain_separated_and_epoch_bound():
    key = mail_key_from_secret(VECTOR["secret"])
    expected = hmac.new(
        VECTOR["secret"].encode(),
        RECOVERY_MAIL_DERIVATION.encode("ascii"),
        hashlib.sha256,
    ).digest()
    assert (
        key == VECTOR["mail_key"] == base64.urlsafe_b64encode(expected).decode("ascii")
    )
    assert key != base64.urlsafe_b64encode(
        hmac.new(
            VECTOR["secret"].encode(),
            RECOVERY_DERIVATION.encode(),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    assert (
        mail_encryption_id(key)
        == VECTOR["encryption_id"]
        == hashlib.sha256(expected).hexdigest()
    )


def test_encrypted_outbox_token_round_trips_and_rejects_wrong_epoch():
    key = mail_key_from_secret(VECTOR["secret"])
    token = "a" * 64
    ciphertext = Fernet(key.encode("ascii")).encrypt(token.encode()).decode("ascii")
    epoch = mail_encryption_id(key)
    assert decrypt_mail_token(ciphertext, key, epoch) == token
    with pytest.raises(ValueError, match="epoch"):
        decrypt_mail_token(ciphertext, mail_key_from_secret("other-secret"), epoch)
    with pytest.raises(ValueError, match="encrypted"):
        decrypt_mail_token("bad-ciphertext", key, epoch)
