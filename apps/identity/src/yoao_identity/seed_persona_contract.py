"""Closed portable contract for trusted seed-persona account creation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pydantic import Field, StrictStr, TypeAdapter, ValidationError, model_validator

from .actor_contract import _json_bytes
from .contract import ClosedModel, IdentityFailure, IdentityUnavailable

SEED_PERSONA_PROTOCOL = "aap-identity-seed-persona/v1"
IDENTITY_SCHEMA = "aap_identity_v1"
SeedPersonaRole: TypeAlias = Literal[
    "super_admin", "project_admin", "employee", "viewer"
]


class _SeedPersonaModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class SeedPersonaRequest(_SeedPersonaModel):
    """Insert-only command preserving the parent seed's User constructor."""

    protocol: Literal["aap-identity-seed-persona/v1"] = Field(alias="schema")
    account_id: uuid.UUID
    email: StrictStr
    name: StrictStr
    password_hash: StrictStr = Field(repr=False)
    role: SeedPersonaRole
    group_name: StrictStr | None


class SeedPersonaReply(_SeedPersonaModel):
    protocol: Literal["aap-identity-seed-persona/v1"] = Field(alias="schema")
    email: StrictStr
    account_id: uuid.UUID


_REQUEST_ADAPTER = TypeAdapter(SeedPersonaRequest)
_REPLY_ADAPTER = TypeAdapter(SeedPersonaReply)


def decode_seed_persona_request(
    payload: dict | bytes | SeedPersonaRequest,
) -> SeedPersonaRequest:
    """Validate the exact creation command before binding it to Native SQL."""
    try:
        return _REQUEST_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityFailure("invalid_request") from None


def decode_seed_persona_reply(
    payload: dict | bytes | SeedPersonaReply,
    request: SeedPersonaRequest,
) -> uuid.UUID:
    """Validate the receipt and bind it to the requested email and account ID."""
    if not isinstance(request, SeedPersonaRequest):
        raise IdentityUnavailable()
    try:
        request = SeedPersonaRequest.model_validate_json(
            request.model_dump_json(by_alias=True)
        )
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None

    if reply.email != request.email or reply.account_id != request.account_id:
        raise IdentityUnavailable()
    return reply.account_id


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    arguments: tuple[tuple[str, str, TypeAdapter[Any]], ...]
    transaction_semantics: str


_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="create_seed_persona_account",
        arguments=(("p_request", "jsonb", _REQUEST_ADAPTER),),
        transaction_semantics=(
            "Insert one seed persona with the supplied account UUID. This is an "
            "insert-only capability and does not authorize the caller, create a "
            "Group, write Audit, commit, retry, or upsert. The trusted caller owns "
            "persona lookup and the surrounding workflow transaction. Native "
            "uniqueness, foreign-key, and length errors propagate."
        ),
    ),
)


def seed_persona_schema() -> dict[str, Any]:
    """Return the wire schema and fixed native capability descriptor."""
    schema = TypeAdapter(SeedPersonaRequest | SeedPersonaReply).json_schema(
        by_alias=True
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SEED_PERSONA_PROTOCOL
    schema["x-native-capabilities"] = {
        "protocol": SEED_PERSONA_PROTOCOL,
        "postgres_schema": IDENTITY_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": (
                    f"{IDENTITY_SCHEMA}.{function.name}"
                    f"({', '.join(argument[1] for argument in function.arguments)})"
                ),
                "arguments": [
                    {
                        "name": name,
                        "postgres_type": postgres_type,
                        "json_schema": adapter.json_schema(),
                    }
                    for name, postgres_type, adapter in function.arguments
                ],
                "result_postgres_type": "jsonb",
                "result_model": SeedPersonaReply.__name__,
                "result_schema_ref": f"#/$defs/{SeedPersonaReply.__name__}",
                "transaction_semantics": function.transaction_semantics,
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "authorization_boundary": (
            "Trusted seed callers retain the production guard and persona lookup; "
            "this fixed function adds no application authorization."
        ),
        "transaction_boundary": "The caller owns the surrounding transaction and commit.",
    }
    return schema


__all__ = [
    "IDENTITY_SCHEMA",
    "SEED_PERSONA_PROTOCOL",
    "SeedPersonaReply",
    "SeedPersonaRequest",
    "SeedPersonaRole",
    "decode_seed_persona_reply",
    "decode_seed_persona_request",
    "seed_persona_schema",
]
