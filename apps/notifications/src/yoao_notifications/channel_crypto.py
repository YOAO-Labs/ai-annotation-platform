"""Fail-closed Fernet encryption for external notification channel Bearer tokens."""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken


class NotificationChannelKeyNotConfigured(RuntimeError):
    """NOTIFICATION_CHANNEL_ENCRYPTION_KEY is absent or invalid."""


class NotificationChannelCryptoError(RuntimeError):
    """Notification channel token ciphertext is malformed or cannot be decrypted."""


def _fernet(key: str | bytes) -> Fernet:
    if not key:
        raise NotificationChannelKeyNotConfigured(
            "NOTIFICATION_CHANNEL_ENCRYPTION_KEY is required"
        )
    try:
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise NotificationChannelKeyNotConfigured(
            "NOTIFICATION_CHANNEL_ENCRYPTION_KEY is invalid"
        ) from exc


def validate_channel_token_plaintext(token: str) -> str:
    if (
        type(token) is not str
        or not token
        or len(token) > 4096
        or token != token.strip()
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)
    ):
        raise ValueError(
            "channel auth_token must be 1..4096 printable ASCII characters"
        )
    return token


def encrypt_channel_token(plaintext: str, key: str | bytes) -> str:
    validated = validate_channel_token_plaintext(plaintext)
    return _fernet(key).encrypt(validated.encode("ascii")).decode("ascii")


def decrypt_channel_token(ciphertext: str, key: str | bytes) -> str:
    if type(ciphertext) is not str or not ciphertext:
        raise NotificationChannelCryptoError("channel auth token ciphertext is empty")
    try:
        raw = ciphertext.encode("ascii")
        plaintext = _fernet(key).decrypt(raw).decode("ascii")
    except NotificationChannelKeyNotConfigured:
        raise
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise NotificationChannelCryptoError(
            "channel auth token could not be decrypted"
        ) from exc
    try:
        return validate_channel_token_plaintext(plaintext)
    except ValueError as exc:
        raise NotificationChannelCryptoError(
            "decrypted channel auth token is malformed"
        ) from exc


def channel_token_sha256(plaintext: str) -> str:
    validated = validate_channel_token_plaintext(plaintext)
    return hashlib.sha256(validated.encode("ascii")).hexdigest()
