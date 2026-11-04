"""Portable manager for principal-bound account deletion commands."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .deletion_contract import (
    DELETION_DERIVATION,
    DELETION_PROTOCOL,
    DeletionFailure,
    DeletionReply,
    DeletionRequest,
    NativeDeletionCommand,
    decode_deletion_reply,
    decode_native_deletion_reply,
    deletion_failure_reply,
    native_deletion_command,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class DeletionTransaction(Protocol):
    async def execute_deletion_command(self, payload: dict) -> dict: ...


class DeletionData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[DeletionTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    if not isinstance(secret, str) or not secret:
        raise IdentityUnavailable()
    try:
        master = hmac.new(
            secret.encode("utf-8"), DELETION_DERIVATION.encode("ascii"), hashlib.sha256
        ).digest()
    except (UnicodeError, TypeError, ValueError):
        raise IdentityUnavailable() from None
    return master, hashlib.sha256(master).hexdigest()


def deletion_request_fingerprint(secret: str, request: DeletionRequest) -> str:
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


def deletion_derivation_id(secret: str) -> str:
    return _derivation(secret)[1]


def _strict_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


class DeletionManager:
    def __init__(self, auth: BoundAuthenticator, data: DeletionData, secret: str):
        self.auth = auth
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: DeletionRequest,
        verified: VerifiedCredential,
        failure: DeletionFailure,
    ) -> None:
        try:
            decode_deletion_reply(
                request,
                deletion_failure_reply(failure),
                expected_principal=verified.principal,
            )
        except DeletionFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: DeletionRequest) -> DeletionReply:
        try:
            verified = await self.auth.authenticate_bound(request.token)
        except IdentityFailure as error:
            status_code = 400 if error.code == "invalid_request" else None
            raise DeletionFailure(error.code, status_code, None) from None

        command: NativeDeletionCommand = native_deletion_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=deletion_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_deletion_command(command_payload)
                native = decode_native_deletion_reply(command, raw)
            except DeletionFailure as failure:
                self._raise_checked_failure(request, verified, failure)

            public_payload = {
                "schema": DELETION_PROTOCOL,
                "ok": True,
                "operation": native.operation,
                "operation_id": str(native.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            _strict_json(public_payload)
            # Both owner-native and public-wire reply validation happen before commit.
            return decode_deletion_reply(
                request,
                public_payload,
                expected_principal=verified.principal,
            )
