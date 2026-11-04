"""Portable, principal-bound manager for Identity directory reads."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .directory_contract import (
    DIRECTORY_DERIVATION,
    DIRECTORY_PROTOCOL,
    DirectoryFailure,
    DirectoryReply,
    DirectoryRequest,
    NativeDirectoryCommand,
    decode_directory_reply,
    decode_native_directory_reply,
    directory_failure_reply,
    native_directory_command,
    normalized_directory_request,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class DirectoryTransaction(Protocol):
    async def execute_directory_command(self, payload: dict) -> dict: ...


class DirectoryData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[DirectoryTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    master = hmac.new(
        secret.encode("utf-8"), DIRECTORY_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return master, hashlib.sha256(master).hexdigest()


def directory_request_fingerprint(secret: str, request: DirectoryRequest) -> str:
    master, _ = _derivation(secret)
    normalized = normalized_directory_request(request)
    canonical = json.dumps(
        {
            "operation": normalized.operation,
            "payload": normalized.payload.model_dump(mode="json", by_alias=True),
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


class DirectoryManager:
    def __init__(
        self, authenticator: BoundAuthenticator, data: DirectoryData, secret: str
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: DirectoryRequest, failure: DirectoryFailure
    ) -> None:
        try:
            decode_directory_reply(request, directory_failure_reply(failure))
        except DirectoryFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: DirectoryRequest) -> DirectoryReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            raise DirectoryFailure(
                error.code,
                400 if error.code == "invalid_request" else None,
                None,
            ) from None

        command: NativeDirectoryCommand = native_directory_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=directory_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_directory_command(command_payload)
                native = decode_native_directory_reply(command, raw)
            except DirectoryFailure as failure:
                self._raise_checked_failure(request, failure)

            reply_payload = {
                "schema": DIRECTORY_PROTOCOL,
                "ok": True,
                "operation": native.operation,
                "operation_id": str(native.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            _strict_json(reply_payload)
            return decode_directory_reply(
                request,
                reply_payload,
                expected_principal=verified.principal,
                replayed=native.replayed,
            )
