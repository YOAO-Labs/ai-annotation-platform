"""Portable manager for principal-bound administrator lifecycle commands."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .suspension_contract import (
    SUSPENSION_DERIVATION,
    SUSPENSION_PROTOCOL,
    NativeSuspensionCommand,
    SuspensionFailure,
    SuspensionReply,
    SuspensionRequest,
    decode_native_suspension_reply,
    decode_suspension_reply,
    native_suspension_command,
    suspension_failure_reply,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class SuspensionTransaction(Protocol):
    async def execute_suspension_command(self, payload: dict) -> dict: ...


class SuspensionData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[SuspensionTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    if not isinstance(secret, str) or not secret:
        raise IdentityUnavailable()
    try:
        master = hmac.new(
            secret.encode("utf-8"),
            SUSPENSION_DERIVATION.encode("ascii"),
            hashlib.sha256,
        ).digest()
    except (UnicodeError, TypeError, ValueError):
        raise IdentityUnavailable() from None
    return master, hashlib.sha256(master).hexdigest()


def suspension_request_fingerprint(secret: str, request: SuspensionRequest) -> str:
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


def suspension_derivation_id(secret: str) -> str:
    return _derivation(secret)[1]


def _strict_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


class SuspensionManager:
    def __init__(self, auth: BoundAuthenticator, data: SuspensionData, secret: str):
        self.auth = auth
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: SuspensionRequest,
        verified: VerifiedCredential,
        failure: SuspensionFailure,
    ) -> None:
        try:
            decode_suspension_reply(
                request,
                suspension_failure_reply(failure),
                expected_principal=verified.principal,
            )
        except SuspensionFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: SuspensionRequest) -> SuspensionReply:
        try:
            verified = await self.auth.authenticate_bound(request.token)
        except IdentityFailure as error:
            status_code = 400 if error.code == "invalid_request" else None
            raise SuspensionFailure(error.code, status_code, None) from None

        command: NativeSuspensionCommand = native_suspension_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=suspension_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_suspension_command(command_payload)
                native = decode_native_suspension_reply(command, raw)
            except SuspensionFailure as failure:
                self._raise_checked_failure(request, verified, failure)

            public_payload = {
                "schema": SUSPENSION_PROTOCOL,
                "ok": True,
                "operation": native.operation,
                "operation_id": str(native.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            _strict_json(public_payload)
            return decode_suspension_reply(
                request,
                public_payload,
                expected_principal=verified.principal,
            )
