"""Portable, principal-bound administrator password-reset manager."""

from __future__ import annotations

import asyncio
import bcrypt
import hashlib
import hmac
import json
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .admin_password_contract import (
    ADMIN_PASSWORD_DERIVATION,
    ADMIN_PASSWORD_PROTOCOL,
    AdminPasswordFailure,
    AdminPasswordReply,
    AdminPasswordRequest,
    NativeAdminPasswordCommand,
    admin_password_failure_reply,
    decode_admin_password_reply,
    decode_native_admin_password_reply,
    native_admin_password_command,
)

_TEMP_PASSWORD_PURPOSE = b"aap-identity-admin-passwords/password/v1\x00"
_TEMP_PASSWORD_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*?"
)
_TEMP_PASSWORD_ACCEPT_LIMIT = 213
_UINT32_LIMIT = 2**32


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class AdminPasswordTransaction(Protocol):
    async def execute_admin_password_command(self, payload: dict) -> dict: ...


class AdminPasswordData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[AdminPasswordTransaction]: ...


def _master(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError("Admin password signing secret must be non-empty")
    return hmac.new(
        secret.encode("utf-8"),
        ADMIN_PASSWORD_DERIVATION.encode("ascii"),
        hashlib.sha256,
    ).digest()


def admin_password_derivation_id(secret: str) -> str:
    return hashlib.sha256(_master(secret)).hexdigest()


def admin_password_request_fingerprint(
    secret: str, request: AdminPasswordRequest
) -> str:
    canonical = json.dumps(
        {
            "operation": request.operation,
            "payload": request.payload.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(_master(secret), canonical, hashlib.sha256).hexdigest()


def derive_admin_password(
    secret: str,
    actor_id,
    operation_id,
    request_fingerprint: str,
) -> str:
    """Derive an unbiased printable password with all four required classes."""
    if not isinstance(actor_id, uuid.UUID) or not isinstance(operation_id, uuid.UUID):
        raise ValueError("Actor and operation identifiers must be UUIDs")
    master = _master(secret)
    try:
        fingerprint_bytes = bytes.fromhex(request_fingerprint)
        if (
            len(fingerprint_bytes) != 32
            or request_fingerprint != fingerprint_bytes.hex()
        ):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Invalid admin password request fingerprint") from None

    for candidate_counter in range(_UINT32_LIMIT):
        characters: list[str] = []
        block_counter = 0
        while len(characters) < 16:
            if block_counter >= _UINT32_LIMIT:
                raise ValueError("Admin password block counter exhausted")
            message = b"".join(
                (
                    _TEMP_PASSWORD_PURPOSE,
                    actor_id.bytes,
                    operation_id.bytes,
                    fingerprint_bytes,
                    candidate_counter.to_bytes(4, "big"),
                    block_counter.to_bytes(4, "big"),
                )
            )
            block = hmac.new(master, message, hashlib.sha256).digest()
            block_counter += 1
            for value in block:
                if value < _TEMP_PASSWORD_ACCEPT_LIMIT:
                    characters.append(
                        _TEMP_PASSWORD_ALPHABET[value % len(_TEMP_PASSWORD_ALPHABET)]
                    )
                    if len(characters) == 16:
                        break
        password = "".join(characters)
        if (
            any("A" <= value <= "Z" for value in password)
            and any("a" <= value <= "z" for value in password)
            and any("0" <= value <= "9" for value in password)
            and any(value in "!@#$%^&*?" for value in password)
        ):
            return password
    raise ValueError("Admin password candidate counter exhausted")


def _hash_password_cost12(password: str) -> str:
    try:
        hashed = bcrypt.hashpw(
            password.encode("ascii"), bcrypt.gensalt(rounds=12)
        ).decode("ascii")
    except (ValueError, TypeError, UnicodeError, RuntimeError):
        raise IdentityUnavailable() from None
    return hashed


def _strict_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


class AdminPasswordManager:
    def __init__(
        self, authenticator: BoundAuthenticator, data: AdminPasswordData, secret: str
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        self.derivation_id = admin_password_derivation_id(secret)

    def _password(self, request: AdminPasswordRequest, actor_id) -> tuple[str, str]:
        fingerprint = admin_password_request_fingerprint(self.secret, request)
        try:
            password = derive_admin_password(
                self.secret, actor_id, request.operation_id, fingerprint
            )
        except (TypeError, ValueError, OverflowError):
            raise IdentityUnavailable() from None
        return fingerprint, password

    @staticmethod
    def _raise_checked_failure(
        request: AdminPasswordRequest,
        verified: VerifiedCredential,
        expected_password: str,
        failure: AdminPasswordFailure,
    ) -> None:
        try:
            decode_admin_password_reply(
                request,
                admin_password_failure_reply(failure),
                expected_principal=verified.principal,
                expected_temp_password=expected_password,
            )
        except AdminPasswordFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: AdminPasswordRequest) -> AdminPasswordReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            if error.code == "invalid_request":
                raise AdminPasswordFailure(error.code, 400, None) from None
            raise AdminPasswordFailure(error.code) from None

        request_fingerprint, password = self._password(
            request, verified.principal.actor_id
        )
        try:
            new_password_hash = await asyncio.to_thread(_hash_password_cost12, password)
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, RuntimeError, UnicodeError):
            raise IdentityUnavailable() from None

        command: NativeAdminPasswordCommand = native_admin_password_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=request_fingerprint,
            derivation_id=self.derivation_id,
            new_password_hash=new_password_hash,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_admin_password_command(command_payload)
                native = decode_native_admin_password_reply(command, raw)
            except AdminPasswordFailure as failure:
                self._raise_checked_failure(request, verified, password, failure)

            wire_payload = {
                "schema": ADMIN_PASSWORD_PROTOCOL,
                "ok": True,
                "operation": native.operation,
                "operation_id": str(native.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": {
                    "target_user_id": str(native.data.target_user_id),
                    "target_email": native.data.target_email,
                    "temp_password": password,
                    "password_admin_reset_at": native.data.password_admin_reset_at.isoformat(),
                },
            }
            _strict_json(wire_payload)
            return decode_admin_password_reply(
                request,
                wire_payload,
                expected_principal=verified.principal,
                expected_temp_password=password,
            )
