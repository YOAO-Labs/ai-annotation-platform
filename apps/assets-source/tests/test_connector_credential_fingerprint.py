import base64
import hashlib
import hmac
import json
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from aap_assets_source.connectors.crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    credential_fingerprint,
    decrypt_secret,
    encrypt_secret,
)


def test_credential_intent_is_stable_keyed_and_bound_to_operation():
    key = Fernet.generate_key()
    operation_id = uuid4()
    secret = {
        "access_key": "AK",
        "secret_key": "do-not-retain",
        "extra": {"a": None, "b": 1.0},
    }
    fingerprint = credential_fingerprint(secret, key, operation_id)
    assert len(fingerprint) == 64
    assert fingerprint == credential_fingerprint(
        dict(reversed(list(secret.items()))), key, operation_id
    )
    assert fingerprint != credential_fingerprint(secret, key, uuid4())
    assert fingerprint != credential_fingerprint(
        secret, Fernet.generate_key(), operation_id
    )
    assert fingerprint != credential_fingerprint(
        {**secret, "secret_key": "rotated"}, key, operation_id
    )
    first, second = encrypt_secret(secret, key), encrypt_secret(secret, key)
    assert first != second
    assert decrypt_secret(first, key) == secret
    assert decrypt_secret(second, key) == secret


def test_credential_intent_framing_is_language_independent():
    key = base64.urlsafe_b64encode(bytes(range(32)))
    operation_id = UUID("12345678-1234-4321-8765-123456789abc")
    secret = {"password": "密码", "passphrase": None}
    canonical = b'{"passphrase":null,"password":"\\u5bc6\\u7801"}'
    assert json.loads(canonical) == secret
    payload = (
        b"aap-assets-connections-request/v1\0"
        + str(operation_id).encode()
        + b"\0"
        + canonical
    )
    assert (
        credential_fingerprint(secret, key, operation_id)
        == hmac.new(bytes(range(32)), payload, hashlib.sha256).hexdigest()
    )


def test_credential_intent_rejects_missing_keys_and_non_json_secrets():
    with pytest.raises(ConnectorCryptoNotConfigured):
        credential_fingerprint({}, "", uuid4())
    with pytest.raises(ConnectorCryptoError):
        credential_fingerprint(
            {"password": float("nan")}, Fernet.generate_key(), uuid4()
        )
