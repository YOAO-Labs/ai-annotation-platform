"""Portable, fenced API-key management command owner."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import uuid
from typing import Protocol

from pydantic import ValidationError

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable
from .key_contract import (
    KEY_DERIVATION,
    KEY_PROTOCOL,
    CreateKeyReply,
    CreateKeyRequest,
    KeyRequest,
    KeyCreatedData,
    KeyFailure,
    KeyListData,
    KeyRecordData,
    KeyRevokedData,
    KeyReply,
    ListKeyReply,
    ListKeyRequest,
    NativeCreateReply,
    NativeKeyReply,
    NativeListReply,
    NativeRevokeReply,
    NativeRotateReply,
    NativeUpdateReply,
    NewKey,
    RevokeKeyReply,
    RevokeKeyRequest,
    RotateKeyReply,
    RotateKeyRequest,
    UpdateKeyReply,
    UpdateKeyRequest,
    decode_native_key_reply,
    native_key_command,
)
from .security import hash_api_key


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class KeyData(Protocol):
    async def key_policy(self) -> dict: ...

    async def execute_key_command(self, payload: dict) -> dict: ...


def derive_key_material(
    secret: str, actor_id: uuid.UUID, operation_id: uuid.UUID
) -> tuple[str, str]:
    master = hmac.new(
        secret.encode("utf-8"), KEY_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    body = (
        b"actor:"
        + str(actor_id).encode("ascii")
        + b":operation:"
        + str(operation_id).encode("ascii")
    )
    digest = hmac.new(master, body, hashlib.sha256).digest()
    plaintext = "ak_" + base64.urlsafe_b64encode(digest[:24]).decode("ascii").rstrip(
        "="
    )
    return plaintext, hashlib.sha256(master).hexdigest()


class KeyManager:
    def __init__(self, authenticator: BoundAuthenticator, data: KeyData, secret: str):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret

    async def execute(self, request: KeyRequest) -> KeyReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            raise KeyFailure(error.code) from None

        material: NewKey | None = None
        plaintext: str | None = None
        if isinstance(request, (CreateKeyRequest, RotateKeyRequest)):
            plaintext, derivation_id = derive_key_material(
                self.secret, verified.principal.actor_id, request.operation_id
            )
            key_id = (
                request.operation_id
                if isinstance(request, CreateKeyRequest)
                else request.payload.key_id
            )
            try:
                key_hash = await asyncio.to_thread(hash_api_key, plaintext)
                material = NewKey(
                    key_id=key_id,
                    key_prefix=plaintext[:12],
                    key_hash=key_hash,
                    derivation_id=derivation_id,
                )
            except (ValueError, TypeError, ValidationError):
                raise IdentityUnavailable() from None

        try:
            command = native_key_command(
                request,
                actor=verified.principal,
                expected_key_hash=verified.key_hash,
                new_key=material,
            )
        except (ValueError, TypeError, ValidationError):
            raise IdentityUnavailable() from None
        native_payload = await self.data.execute_key_command(command)
        native_reply = decode_native_key_reply(
            request,
            native_payload,
            expected_principal=verified.principal,
            new_key=material,
        )
        return self._wire_reply(request, native_reply, plaintext)

    @staticmethod
    def _wire_reply(
        request: KeyRequest, reply: NativeKeyReply, plaintext: str | None
    ) -> KeyReply:
        shared = {
            "schema": KEY_PROTOCOL,
            "ok": True,
            "operation_id": reply.operation_id,
            "principal": reply.principal,
        }
        if isinstance(reply, NativeListReply) and isinstance(request, ListKeyRequest):
            return ListKeyReply(
                **shared,
                operation="list_api_keys",
                data=KeyListData(items=reply.data.items),
            )
        if isinstance(reply, NativeCreateReply) and isinstance(
            request, CreateKeyRequest
        ):
            if plaintext is None:
                raise IdentityUnavailable()
            return CreateKeyReply(
                **shared,
                operation="create_api_key",
                data=KeyCreatedData(key=reply.data.key, plaintext=plaintext),
            )
        if isinstance(reply, NativeUpdateReply) and isinstance(
            request, UpdateKeyRequest
        ):
            return UpdateKeyReply(
                **shared,
                operation="update_api_key",
                data=KeyRecordData(key=reply.data.key),
            )
        if isinstance(reply, NativeRotateReply) and isinstance(
            request, RotateKeyRequest
        ):
            if plaintext is None:
                raise IdentityUnavailable()
            return RotateKeyReply(
                **shared,
                operation="rotate_api_key",
                data=KeyCreatedData(key=reply.data.key, plaintext=plaintext),
            )
        if isinstance(reply, NativeRevokeReply) and isinstance(
            request, RevokeKeyRequest
        ):
            return RevokeKeyReply(
                **shared,
                operation="revoke_api_key",
                data=KeyRevokedData(
                    key_id=reply.data.key_id, revoked=reply.data.revoked
                ),
            )
        raise IdentityUnavailable()
