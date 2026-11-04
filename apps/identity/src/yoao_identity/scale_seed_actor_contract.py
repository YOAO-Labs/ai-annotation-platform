"""Closed contract for the two actor reads used by the scale seed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .actor_contract import _json_bytes
from .contract import ClosedModel, IdentityUnavailable

SCALE_SEED_ACTOR_PROTOCOL = "aap-identity-scale-seed-actors/v1"
IDENTITY_SCHEMA = "aap_identity_v1"
ScaleSeedActorKind = Literal["super_admin", "annotator"]


class ScaleSeedActorReply(ClosedModel):
    """One exact result shape shared by both fixed Identity owner selectors."""

    protocol: Literal["aap-identity-scale-seed-actors/v1"] = Field(alias="schema")
    kind: ScaleSeedActorKind
    account_id: uuid.UUID | None

    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


_REPLY_ADAPTER = TypeAdapter(ScaleSeedActorReply)


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    signature: str
    argument_name: str | None
    argument_type: str | None
    argument_schema: dict[str, Any] | None
    kind: ScaleSeedActorKind
    transaction_semantics: str


_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="read_scale_seed_super_admin",
        signature="aap_identity_v1.read_scale_seed_super_admin()",
        argument_name=None,
        argument_type=None,
        argument_schema=None,
        kind="super_admin",
        transaction_semantics=(
            "Select the earliest-created public.users row with role super_admin. "
            "Do not filter is_active or add a tie-breaker; equal created_at values "
            "remain unspecified. This read-only STABLE function does not authorize "
            "the caller or commit its transaction."
        ),
    ),
    _NativeFunction(
        name="read_scale_seed_annotator",
        signature="aap_identity_v1.read_scale_seed_annotator(uuid[])",
        argument_name="p_candidate_ids",
        argument_type="uuid[]",
        argument_schema={
            "type": ["array", "null"],
            "items": {"type": "string", "format": "uuid"},
        },
        kind="annotator",
        transaction_semantics=(
            "Select the earliest-created active public.users row whose ID is in the "
            "caller-supplied annotator candidate UUID array. Do not apply a platform "
            "role filter or add a tie-breaker; equal created_at values remain "
            "unspecified. The function does not read Workspace membership or project "
            "tables. The caller selects project_member_facts candidates and invokes "
            "this STABLE function in the same SQL statement, preserving one statement "
            "snapshot across candidate and account reads. This function does not "
            "authorize the caller or commit its transaction."
        ),
    ),
)


def decode_scale_seed_actor_reply(
    payload: dict | str | bytes | bytearray | memoryview | ScaleSeedActorReply,
    expected_kind: ScaleSeedActorKind,
) -> uuid.UUID | None:
    """Validate a native reply and bind it to the requested selector kind."""
    if type(expected_kind) is not str or expected_kind not in (
        "super_admin",
        "annotator",
    ):
        raise IdentityUnavailable()

    try:
        if isinstance(payload, str):
            normalized = payload.encode("utf-8")
        elif isinstance(payload, (bytearray, memoryview)):
            normalized = bytes(payload)
        else:
            normalized = payload
        encoded = _json_bytes(normalized)
        reply = _REPLY_ADAPTER.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None

    if reply.kind != expected_kind:
        raise IdentityUnavailable()
    return reply.account_id


def scale_seed_actor_schema() -> dict[str, Any]:
    """Return the closed wire schema and fixed native read capabilities."""
    reply_schema = _REPLY_ADAPTER.json_schema(by_alias=True)
    reply_definition = {
        key: value for key, value in reply_schema.items() if key != "$defs"
    }
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCALE_SEED_ACTOR_PROTOCOL,
        "$ref": f"#/$defs/{ScaleSeedActorReply.__name__}",
        "$defs": {ScaleSeedActorReply.__name__: reply_definition},
    }
    schema["x-native-capabilities"] = {
        "protocol": SCALE_SEED_ACTOR_PROTOCOL,
        "postgres_schema": IDENTITY_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": function.signature,
                "arguments": (
                    []
                    if function.argument_name is None
                    else [
                        {
                            "name": function.argument_name,
                            "postgres_type": function.argument_type,
                            "json_schema": function.argument_schema,
                        }
                    ]
                ),
                "result_postgres_type": "jsonb",
                "result_model": ScaleSeedActorReply.__name__,
                "result_schema_ref": f"#/$defs/{ScaleSeedActorReply.__name__}",
                "result_kind": function.kind,
                "transaction_semantics": function.transaction_semantics,
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "authorization_boundary": (
            "Trusted scale-seed callers retain actor selection policy and the "
            "surrounding workflow; these fixed read functions add no authorization."
        ),
        "transaction_boundary": (
            "The caller owns the transaction. For annotator selection, the candidate "
            "UUID array must be selected from Workspace facts inside the same SQL "
            "statement that invokes the Identity function."
        ),
    }
    return schema


__all__ = [
    "IDENTITY_SCHEMA",
    "SCALE_SEED_ACTOR_PROTOCOL",
    "ScaleSeedActorReply",
    "decode_scale_seed_actor_reply",
    "scale_seed_actor_schema",
]
