"""Connector-secret encryption without application configuration imports."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


class ConnectorCryptoNotConfigured(RuntimeError):
    """The explicit connector encryption key is absent or invalid."""


class ConnectorCryptoError(RuntimeError):
    """The connector secret is malformed or does not match the configured key."""


def _fernet(key: str | bytes) -> Fernet:
    if not key:
        raise ConnectorCryptoNotConfigured("CONNECTOR_ENCRYPTION_KEY is required")
    try:
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ConnectorCryptoNotConfigured(
            "CONNECTOR_ENCRYPTION_KEY is invalid"
        ) from exc


def encrypt_secret(secret: dict, key: str | bytes) -> bytes:
    """Encrypt a connector credential object using the explicit Fernet key."""
    if not isinstance(secret, dict):
        raise ConnectorCryptoError("connector secret must be an object")
    try:
        plaintext = json.dumps(
            secret, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConnectorCryptoError("connector secret is not safe JSON") from exc
    return _fernet(key).encrypt(plaintext)


def decrypt_secret(token: bytes | str, key: str | bytes) -> dict:
    """Decrypt a Fernet token and require a JSON object payload."""
    if isinstance(token, str):
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConnectorCryptoError("connector secret token is malformed") from exc
    elif isinstance(token, bytes):
        token_bytes = token
    else:
        raise ConnectorCryptoError("connector secret token is malformed")
    try:
        plaintext = _fernet(key).decrypt(token_bytes)
        value = json.loads(plaintext, parse_constant=_reject_json_constant)
    except InvalidToken as exc:
        raise ConnectorCryptoError("connector secret could not be decrypted") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConnectorCryptoError("connector secret payload is malformed") from exc
    if not isinstance(value, dict):
        raise ConnectorCryptoError("connector secret payload must be an object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
