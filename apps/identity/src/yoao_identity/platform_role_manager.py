"""Portable, principal-bound platform-role preview and mutation manager."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .platform_role_contract import (
    PLATFORM_ROLE_DERIVATION,
    PLATFORM_ROLE_PROTOCOL,
    NativePlatformRoleCommand,
    PlatformRoleFailure,
    PlatformRoleReply,
    PlatformRoleRequest,
    decode_native_platform_role_reply,
    decode_platform_role_reply,
    native_platform_role_command,
    platform_role_failure_reply,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class PlatformRoleTransaction(Protocol):
    async def execute_platform_role_command(self, payload: dict) -> dict: ...


class PlatformRoleData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[PlatformRoleTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    if not isinstance(secret, str) or not secret:
        raise IdentityUnavailable()
    try:
        master = hmac.new(
            secret.encode("utf-8"),
            PLATFORM_ROLE_DERIVATION.encode("ascii"),
            hashlib.sha256,
        ).digest()
    except (UnicodeError, TypeError, ValueError):
        raise IdentityUnavailable() from None
    return master, hashlib.sha256(master).hexdigest()


def platform_role_request_fingerprint(secret: str, request: PlatformRoleRequest) -> str:
    master, _ = _derivation(secret)
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
    return hmac.new(master, canonical, hashlib.sha256).hexdigest()


def platform_role_derivation_id(secret: str) -> str:
    return _derivation(secret)[1]


def _strict_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


class PlatformRoleManager:
    def __init__(
        self,
        authenticator: BoundAuthenticator,
        data: PlatformRoleData,
        secret: str,
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: PlatformRoleRequest,
        verified: VerifiedCredential,
        failure: PlatformRoleFailure,
    ) -> None:
        try:
            decode_platform_role_reply(
                request,
                platform_role_failure_reply(failure),
                expected_principal=verified.principal,
            )
        except PlatformRoleFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: PlatformRoleRequest) -> PlatformRoleReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            status_code = 400 if error.code == "invalid_request" else None
            raise PlatformRoleFailure(error.code, status_code, None) from None

        command: NativePlatformRoleCommand = native_platform_role_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=platform_role_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_platform_role_command(command_payload)
                native = decode_native_platform_role_reply(command, raw)
            except PlatformRoleFailure as failure:
                self._raise_checked_failure(request, verified, failure)

            reply_payload = {
                "schema": PLATFORM_ROLE_PROTOCOL,
                "ok": True,
                "operation": native.operation,
                "operation_id": str(native.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            _strict_json(reply_payload)
            return decode_platform_role_reply(
                request,
                reply_payload,
                expected_principal=verified.principal,
            )
