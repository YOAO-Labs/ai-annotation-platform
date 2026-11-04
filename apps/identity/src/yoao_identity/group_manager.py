"""Portable, principal-bound manager for Identity group commands."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .group_contract import (
    GROUP_DERIVATION,
    GROUP_PROTOCOL,
    GroupFailure,
    GroupReply,
    GroupRequest,
    NativeGroupCommand,
    decode_group_reply,
    decode_native_group_reply,
    group_failure_reply,
    native_group_command,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class GroupTransaction(Protocol):
    async def execute_group_command(self, payload: dict) -> dict: ...


class GroupData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[GroupTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    master = hmac.new(
        secret.encode("utf-8"), GROUP_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return master, hashlib.sha256(master).hexdigest()


def group_request_fingerprint(secret: str, request: GroupRequest) -> str:
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


class GroupManager:
    def __init__(self, authenticator: BoundAuthenticator, data: GroupData, secret: str):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(request: GroupRequest, failure: GroupFailure) -> None:
        try:
            decode_group_reply(request, group_failure_reply(failure))
        except GroupFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: GroupRequest) -> GroupReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            raise GroupFailure(error.code) from None

        command: NativeGroupCommand = native_group_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=group_request_fingerprint(self.secret, request),
            derivation_id=self.derivation_id,
        )
        command_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(command_payload)

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_group_command(command_payload)
                native = decode_native_group_reply(command, raw)
            except GroupFailure as failure:
                self._raise_checked_failure(request, failure)

            reply_payload = {
                "schema": GROUP_PROTOCOL,
                "ok": True,
                "operation": request.operation,
                "operation_id": str(request.operation_id),
                "principal": native.principal.model_dump(mode="json"),
                "data": native.data.model_dump(mode="json", by_alias=True),
            }
            return decode_group_reply(
                request, reply_payload, expected_principal=verified.principal
            )
