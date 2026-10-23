"""Closed wire/native contracts for Identity API-key management commands."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .contract import (
    FailureCode,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
)

KEY_PROTOCOL = "aap-identity-keys/v1"
NATIVE_KEY_PROTOCOL = "aap-identity-keys-native/v1"
KEY_DERIVATION = "aap-identity-api-keys/v1"

KeyOperation = Literal[
    "list_api_keys",
    "create_api_key",
    "update_api_key",
    "rotate_api_key",
    "revoke_api_key",
]
KeyFailureCode: TypeAlias = (
    FailureCode | Literal["key_not_found", "key_busy", "operation_conflict"]
)

_PREFIX_PATTERN = r"^ak_[A-Za-z0-9_-]{9}$"
_PLAINTEXT_PATTERN = r"^ak_[A-Za-z0-9_-]{32}$"
_BCRYPT12_PATTERN = r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$"
_HEX_64_PATTERN = r"^[0-9a-f]{64}$"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EmptyPayload(ClosedModel):
    pass


class CreateKeyPayload(ClosedModel):
    name: str = Field(min_length=1, max_length=60, strict=True)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650, strict=True)


class KeyPatch(ClosedModel):
    name: str | None = Field(default=None, min_length=1, max_length=60, strict=True)
    scopes: list[str] | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650, strict=True)

    @model_validator(mode="after")
    def reject_null_identity_fields(self) -> KeyPatch:
        for field in ("name", "scopes"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to leave unchanged")
        return self


class UpdateKeyPayload(ClosedModel):
    key_id: uuid.UUID
    patch: KeyPatch


class TargetKeyPayload(ClosedModel):
    key_id: uuid.UUID


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-keys/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)


class ListKeyRequest(_WireRequest):
    operation: Literal["list_api_keys"]
    payload: EmptyPayload


class CreateKeyRequest(_WireRequest):
    operation: Literal["create_api_key"]
    payload: CreateKeyPayload


class UpdateKeyRequest(_WireRequest):
    operation: Literal["update_api_key"]
    payload: UpdateKeyPayload


class RotateKeyRequest(_WireRequest):
    operation: Literal["rotate_api_key"]
    payload: TargetKeyPayload


class RevokeKeyRequest(_WireRequest):
    operation: Literal["revoke_api_key"]
    payload: TargetKeyPayload


KeyRequest: TypeAlias = Annotated[
    ListKeyRequest
    | CreateKeyRequest
    | UpdateKeyRequest
    | RotateKeyRequest
    | RevokeKeyRequest,
    Field(discriminator="operation"),
]
_KEY_REQUESTS = TypeAdapter(KeyRequest)


def _json_bytes(payload: dict | bytes) -> bytes:
    if isinstance(payload, bytes):
        _reject_protocol_alias(json.loads(payload))
        return payload
    _reject_protocol_alias(payload)
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def decode_key_request(payload: dict | bytes) -> KeyRequest:
    """Decode an exact public command request; Pydantic errors identify bad input."""
    return _KEY_REQUESTS.validate_json(_json_bytes(payload))


class NewKey(ClosedModel):
    key_id: uuid.UUID
    key_prefix: str = Field(pattern=_PREFIX_PATTERN, strict=True)
    key_hash: str = Field(pattern=_BCRYPT12_PATTERN, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64_PATTERN, strict=True)


class KeyRecord(ClosedModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=60, strict=True)
    key_prefix: str = Field(pattern=_PREFIX_PATTERN, strict=True)
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @field_validator("expires_at", "last_used_at", "revoked_at", "created_at")
    @classmethod
    def aware_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Key timestamps must include a timezone")
            return value.astimezone(UTC)
        return None


class KeyListData(ClosedModel):
    items: list[KeyRecord]


class KeyRecordData(ClosedModel):
    key: KeyRecord


class KeyCreatedData(ClosedModel):
    key: KeyRecord
    plaintext: str = Field(pattern=_PLAINTEXT_PATTERN, strict=True, repr=False)

    @model_validator(mode="after")
    def prefix_matches_plaintext(self) -> KeyCreatedData:
        if self.key.key_prefix != self.plaintext[:12]:
            raise ValueError("Key prefix does not match plaintext")
        return self


class KeyRevokedData(ClosedModel):
    key_id: uuid.UUID
    revoked: Literal[True]

    @field_validator("revoked", mode="before")
    @classmethod
    def boolean_revocation(cls, value):
        if type(value) is not bool:
            raise ValueError("Revocation must be a boolean")
        return value


class KeyFailureDetail(ClosedModel):
    code: KeyFailureCode


class KeyFailureReply(ClosedModel):
    protocol: Literal["aap-identity-keys/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: KeyFailureDetail


class _WireSuccess(ClosedModel):
    protocol: Literal["aap-identity-keys/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class ListKeyReply(_WireSuccess):
    operation: Literal["list_api_keys"]
    data: KeyListData


class CreateKeyReply(_WireSuccess):
    operation: Literal["create_api_key"]
    data: KeyCreatedData


class UpdateKeyReply(_WireSuccess):
    operation: Literal["update_api_key"]
    data: KeyRecordData


class RotateKeyReply(_WireSuccess):
    operation: Literal["rotate_api_key"]
    data: KeyCreatedData


class RevokeKeyReply(_WireSuccess):
    operation: Literal["revoke_api_key"]
    data: KeyRevokedData


KeyReply: TypeAlias = Annotated[
    ListKeyReply | CreateKeyReply | UpdateKeyReply | RotateKeyReply | RevokeKeyReply,
    Field(discriminator="operation"),
]
_KEY_REPLIES = TypeAdapter(KeyReply)


class KeyFailure(Exception):
    def __init__(self, code: KeyFailureCode):
        self.code = code
        super().__init__(code)


def key_failure_reply(code: KeyFailureCode) -> dict:
    return KeyFailureReply(
        schema=KEY_PROTOCOL, ok=False, error=KeyFailureDetail(code=code)
    ).model_dump(mode="json", by_alias=True)


def _raise_failure_or_invalid(raw: dict, encoded: bytes, schema: str) -> None:
    if type(raw.get("ok")) is not bool:
        raise IdentityUnavailable()
    if raw["ok"]:
        return
    try:
        error = KeyFailureReply.model_validate_json(encoded)
    except ValidationError:
        raise IdentityUnavailable() from None
    if error.protocol != schema:
        raise IdentityUnavailable()
    raise KeyFailure(error.error.code)


def _decode_key_reply(payload: dict | bytes) -> KeyReply:
    encoded = _json_bytes(payload)
    raw = json.loads(encoded)
    if not isinstance(raw, dict):
        raise IdentityUnavailable()
    _raise_failure_or_invalid(raw, encoded, KEY_PROTOCOL)
    try:
        return _KEY_REPLIES.validate_json(encoded)
    except (ValidationError, ValueError, TypeError):
        raise IdentityUnavailable() from None


def _validate_reply_target(request: KeyRequest, reply: KeyReply) -> None:
    if (
        reply.operation != request.operation
        or reply.operation_id != request.operation_id
    ):
        raise IdentityUnavailable()
    if isinstance(request, CreateKeyRequest):
        if (
            not isinstance(reply, CreateKeyReply)
            or reply.data.key.id != request.operation_id
        ):
            raise IdentityUnavailable()
    elif isinstance(request, UpdateKeyRequest):
        if (
            not isinstance(reply, UpdateKeyReply)
            or reply.data.key.id != request.payload.key_id
        ):
            raise IdentityUnavailable()
    elif isinstance(request, RotateKeyRequest):
        if (
            not isinstance(reply, RotateKeyReply)
            or reply.data.key.id != request.payload.key_id
        ):
            raise IdentityUnavailable()
    elif isinstance(request, RevokeKeyRequest):
        if (
            not isinstance(reply, RevokeKeyReply)
            or reply.data.key_id != request.payload.key_id
        ):
            raise IdentityUnavailable()
    elif not isinstance(reply, ListKeyReply):
        raise IdentityUnavailable()


def decode_key_reply(request: KeyRequest, payload: dict | bytes) -> KeyReply:
    """Decode a public reply and bind it to the request operation and target."""
    try:
        reply = _decode_key_reply(payload)
        _validate_reply_target(request, reply)
        return reply
    except KeyFailure:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


class _NativeCommand(ClosedModel):
    protocol: Literal["aap-identity-keys-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal
    expected_key_hash: str | None = Field(
        min_length=1, max_length=255, strict=True, repr=False
    )

    @model_validator(mode="after")
    def expected_hash_matches_actor_kind(self):
        if self.actor.credential_type == "jwt":
            if self.expected_key_hash is not None:
                raise ValueError("JWT command cannot include an API-key hash")
        elif self.expected_key_hash is None:
            raise ValueError("API-key command needs its verified hash")
        return self


class NativeListCommand(_NativeCommand):
    operation: Literal["list_api_keys"]
    payload: EmptyPayload
    new_key: None


class NativeCreateCommand(_NativeCommand):
    operation: Literal["create_api_key"]
    payload: CreateKeyPayload
    new_key: NewKey

    @model_validator(mode="after")
    def creation_key_uses_operation_id(self):
        if self.new_key.key_id != self.operation_id:
            raise ValueError("Create key id must equal operation id")
        return self


class NativeUpdateCommand(_NativeCommand):
    operation: Literal["update_api_key"]
    payload: UpdateKeyPayload
    new_key: None


class NativeRotateCommand(_NativeCommand):
    operation: Literal["rotate_api_key"]
    payload: TargetKeyPayload
    new_key: NewKey

    @model_validator(mode="after")
    def rotation_key_uses_target_id(self):
        if self.new_key.key_id != self.payload.key_id:
            raise ValueError("Rotation key id must equal target id")
        return self


class NativeRevokeCommand(_NativeCommand):
    operation: Literal["revoke_api_key"]
    payload: TargetKeyPayload
    new_key: None


NativeKeyCommand: TypeAlias = Annotated[
    NativeListCommand
    | NativeCreateCommand
    | NativeUpdateCommand
    | NativeRotateCommand
    | NativeRevokeCommand,
    Field(discriminator="operation"),
]
_NATIVE_KEY_COMMANDS = TypeAdapter(NativeKeyCommand)


def decode_native_key_command(payload: dict | bytes) -> NativeKeyCommand:
    try:
        return _NATIVE_KEY_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def native_key_command(
    request: KeyRequest,
    *,
    actor: Principal,
    expected_key_hash: str | None,
    new_key: NewKey | None,
) -> dict:
    command_types = {
        "list_api_keys": NativeListCommand,
        "create_api_key": NativeCreateCommand,
        "update_api_key": NativeUpdateCommand,
        "rotate_api_key": NativeRotateCommand,
        "revoke_api_key": NativeRevokeCommand,
    }
    payload = request.payload
    if isinstance(request, CreateKeyRequest):
        # Materialize create defaults so omitted and explicit defaults share one receipt scope.
        payload = CreateKeyPayload(
            name=payload.name,
            scopes=list(payload.scopes),
            expires_in_days=payload.expires_in_days,
        )
    command = command_types[request.operation].model_validate(
        {
            "schema": NATIVE_KEY_PROTOCOL,
            "operation": request.operation,
            "operation_id": request.operation_id,
            "actor": actor,
            "expected_key_hash": expected_key_hash,
            "payload": payload,
            "new_key": new_key,
        }
    )
    return command.model_dump(mode="json", by_alias=True, exclude_unset=True)


class NativeKeyListData(ClosedModel):
    items: list[KeyRecord]


class NativeKeyRecordData(ClosedModel):
    key: KeyRecord


class NativeKeyRevokedData(KeyRevokedData):
    pass


class _NativeSuccess(ClosedModel):
    protocol: Literal["aap-identity-keys-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class NativeListReply(_NativeSuccess):
    operation: Literal["list_api_keys"]
    data: NativeKeyListData


class NativeCreateReply(_NativeSuccess):
    operation: Literal["create_api_key"]
    data: NativeKeyRecordData


class NativeUpdateReply(_NativeSuccess):
    operation: Literal["update_api_key"]
    data: NativeKeyRecordData


class NativeRotateReply(_NativeSuccess):
    operation: Literal["rotate_api_key"]
    data: NativeKeyRecordData


class NativeRevokeReply(_NativeSuccess):
    operation: Literal["revoke_api_key"]
    data: NativeKeyRevokedData


NativeKeyReply: TypeAlias = Annotated[
    NativeListReply
    | NativeCreateReply
    | NativeUpdateReply
    | NativeRotateReply
    | NativeRevokeReply,
    Field(discriminator="operation"),
]
_NATIVE_KEY_REPLIES = TypeAdapter(NativeKeyReply)


class NativeKeyFailureReply(ClosedModel):
    protocol: Literal["aap-identity-keys-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: KeyFailureDetail


def _native_target_id(request: KeyRequest) -> uuid.UUID | None:
    if isinstance(request, CreateKeyRequest):
        return request.operation_id
    if isinstance(request, (UpdateKeyRequest, RotateKeyRequest, RevokeKeyRequest)):
        return request.payload.key_id
    return None


def _key_record_from_native(reply: NativeKeyReply) -> KeyRecord | None:
    if isinstance(reply, NativeRevokeReply):
        return None
    if isinstance(reply, NativeListReply):
        return None
    return reply.data.key


def _decode_native_reply(payload: dict | bytes) -> NativeKeyReply:
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure = NativeKeyFailureReply.model_validate_json(encoded)
            raise KeyFailure(failure.error.code)
        return _NATIVE_KEY_REPLIES.validate_json(encoded)
    except KeyFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _same_fenced_credential(actual: Principal, expected: Principal) -> bool:
    same_identity = (
        actual.actor_id == expected.actor_id
        and actual.credential_type == expected.credential_type
        and actual.credential_id == expected.credential_id
        and actual.generation == expected.generation
    )
    return same_identity and (
        expected.credential_type != "jwt" or actual.expires_at == expected.expires_at
    )


def _validate_native_binding(
    reply: NativeKeyReply,
    *,
    operation: str,
    operation_id: uuid.UUID,
    expected_principal: Principal,
    target_id: uuid.UUID | None,
    new_key: NewKey | None,
) -> None:
    if reply.operation != operation or reply.operation_id != operation_id:
        raise IdentityUnavailable()
    if not _same_fenced_credential(reply.principal, expected_principal):
        raise IdentityUnavailable()
    if isinstance(reply, NativeRevokeReply):
        if target_id is None or reply.data.key_id != target_id:
            raise IdentityUnavailable()
        return
    key = _key_record_from_native(reply)
    if target_id is not None and (key is None or key.id != target_id):
        raise IdentityUnavailable()
    if operation in ("create_api_key", "rotate_api_key"):
        if new_key is None or key is None or key.key_prefix != new_key.key_prefix:
            raise IdentityUnavailable()


def decode_native_key_command_reply(
    command: NativeKeyCommand, payload: dict | bytes
) -> NativeKeyReply:
    """Validate a command reply while the native data transaction is still open."""
    reply = _decode_native_reply(payload)
    if isinstance(command, NativeCreateCommand):
        target_id, new_key = command.operation_id, command.new_key
    elif isinstance(command, (NativeUpdateCommand, NativeRotateCommand)):
        target_id = command.payload.key_id
        new_key = command.new_key if isinstance(command, NativeRotateCommand) else None
    elif isinstance(command, NativeRevokeCommand):
        target_id, new_key = command.payload.key_id, None
    else:
        target_id, new_key = None, None
    _validate_native_binding(
        reply,
        operation=command.operation,
        operation_id=command.operation_id,
        expected_principal=command.actor,
        target_id=target_id,
        new_key=new_key,
    )
    return reply


def decode_native_key_reply(
    request: KeyRequest,
    payload: dict | bytes,
    *,
    expected_principal: Principal,
    new_key: NewKey | None,
) -> NativeKeyReply:
    """Decode the native reply, enforce actor fencing identity and target binding."""
    reply = _decode_native_reply(payload)
    _validate_native_binding(
        reply,
        operation=request.operation,
        operation_id=request.operation_id,
        expected_principal=expected_principal,
        target_id=_native_target_id(request),
        new_key=new_key,
    )
    return reply
