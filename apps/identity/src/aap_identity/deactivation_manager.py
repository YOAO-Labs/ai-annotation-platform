"""Portable manager for principal-bound self-deactivation commands."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .deactivation_contract import (
    DEACTIVATION_DERIVATION,
    DEACTIVATION_PROTOCOL,
    DeactivationFailure,
    DeactivationReply,
    DeactivationRequest,
    NativeDeactivationCommand,
    decode_deactivation_reply,
    decode_native_deactivation_reply,
    deactivation_failure_reply,
    native_deactivation_command,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class DeactivationTransaction(Protocol):
    async def execute_deactivation_command(self, payload: dict) -> dict: ...


class DeactivationData(Protocol):
    def transaction(
        self,
    ) -> AbstractAsyncContextManager[DeactivationTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    master = hmac.new(
        secret.encode("utf-8"), DEACTIVATION_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return master, hashlib.sha256(master).hexdigest()


def deactivation_request_fingerprint(secret: str, request: DeactivationRequest) -> str:
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


def _strict_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


class DeactivationManager:
    def __init__(
        self,
        authenticator: BoundAuthenticator,
        data: DeactivationData,
        secret: str,
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: DeactivationRequest, failure: DeactivationFailure
    ) -> None:
        try:
            decode_deactivation_reply(request, deactivation_failure_reply(failure))
        except DeactivationFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: DeactivationRequest) -> DeactivationReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            raise DeactivationFailure(error.code) from None

        command: NativeDeactivationCommand = native_deactivation_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=deactivation_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_deactivation_command(command_payload)
                native = decode_native_deactivation_reply(command, raw)
            except DeactivationFailure as failure:
                self._raise_checked_failure(request, failure)

            public_payload = {
                "schema": DEACTIVATION_PROTOCOL,
                "ok": True,
                "operation": request.operation,
                "operation_id": str(request.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            return decode_deactivation_reply(
                request,
                public_payload,
                expected_principal=verified.principal,
            )
