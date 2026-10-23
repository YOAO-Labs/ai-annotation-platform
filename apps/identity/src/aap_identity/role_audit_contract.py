"""Portable read contract for the historical project-role audit."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictStr, TypeAdapter, field_validator

from .contract import ClosedModel

ROLE_AUDIT_PROTOCOL = "aap-identity-role-audit/v1"
ROLE_AUDIT_SCHEMA = "aap_identity_audit_v1"
ROLE_AUDIT_VIEWS = (
    "account_roles",
    "invitation_facts",
    "schema_capabilities",
)

RoleValue = Annotated[StrictStr, Field(max_length=50)]
InvitationRoleValue = Annotated[StrictStr, Field(max_length=32)]


class RoleAuditAccount(ClosedModel):
    """One account row, retaining unknown and nullable diagnostic values."""

    id: uuid.UUID
    role: RoleValue | None
    is_active: StrictBool | None


class RoleAuditInvitation(ClosedModel):
    """Invitation fields used by the audit, with legacy role absence as NULL."""

    id: uuid.UUID
    project_id: uuid.UUID | None
    role: InvitationRoleValue
    project_role: StrictStr | None
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    invited_by: uuid.UUID

    @field_validator("expires_at", "accepted_at", "revoked_at")
    @classmethod
    def aware_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Invitation timestamps must include a timezone")
            return value.astimezone(UTC)
        return None


class RoleAuditSchemaCapabilities(ClosedModel):
    """Whether the optional project-role column exists in this schema."""

    invitation_project_role: StrictBool


_ROW_MODELS = (
    RoleAuditAccount,
    RoleAuditInvitation,
    RoleAuditSchemaCapabilities,
)


def _relation_columns(
    model: type[ClosedModel],
    postgres_types: tuple[str, ...],
    nullability: tuple[bool, ...],
) -> list[dict]:
    model_schema = TypeAdapter(model).json_schema()
    fields = model_schema["properties"]
    return [
        {
            "name": name,
            "postgres_type": postgres_type,
            "nullable": nullable,
            "json_schema": fields[name],
        }
        for (name, _), postgres_type, nullable in zip(
            model.model_fields.items(), postgres_types, nullability, strict=True
        )
    ]


def role_audit_schema() -> dict[str, Any]:
    """Describe the fixed, unfiltered Identity-owned audit relations."""
    definitions = {
        model.__name__: TypeAdapter(model).json_schema() for model in _ROW_MODELS
    }
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ROLE_AUDIT_PROTOCOL,
        "$defs": definitions,
        "title": "Identity project-role audit relation rows",
        "oneOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in _ROW_MODELS],
        "x-postgres-read-relations": {
            "protocol": ROLE_AUDIT_PROTOCOL,
            "relations": [
                {
                    "name": ROLE_AUDIT_VIEWS[0],
                    "qualified_name": f"{ROLE_AUDIT_SCHEMA}.{ROLE_AUDIT_VIEWS[0]}",
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        RoleAuditAccount,
                        ("uuid", "character varying(50)", "boolean"),
                        (False, True, True),
                    ),
                    "semantics": (
                        "One row for every public.users account, with no filtering, "
                        "role mapping, sorting, deduplication, or join. Unknown role "
                        "strings and nullable role/activity diagnostics are preserved. "
                        "The caller joins this relation inside its existing SQL "
                        "statements and owns the transaction snapshot."
                    ),
                },
                {
                    "name": "invitation_facts",
                    "qualified_name": f"{ROLE_AUDIT_SCHEMA}.invitation_facts",
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        RoleAuditInvitation,
                        (
                            "uuid",
                            "uuid",
                            "character varying(32)",
                            "text",
                            "timestamp with time zone",
                            "timestamp with time zone",
                            "timestamp with time zone",
                            "uuid",
                        ),
                        (False, True, False, True, False, True, True, False),
                    ),
                    "semantics": (
                        "One row for every public.user_invitations row, without "
                        "filters or ordering. project_role is extracted from the "
                        "whole-row JSON value, so a column absent on legacy schemas "
                        "reads as NULL and becomes visible after its additive column "
                        "is installed without reapplying this installer. The relation "
                        "never returns email, token, inviter profile, or accepted-user "
                        "fields; it returns only the invited_by UUID required by the "
                        "historical report."
                    ),
                },
                {
                    "name": "schema_capabilities",
                    "qualified_name": f"{ROLE_AUDIT_SCHEMA}.schema_capabilities",
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        RoleAuditSchemaCapabilities,
                        ("boolean",),
                        (False,),
                    ),
                    "semantics": (
                        "Exactly one row reporting whether the optional "
                        "user_invitations.project_role column exists. The value is "
                        "computed from pg_catalog.pg_attribute and changes after the "
                        "additive migration without reinstalling this relation."
                    ),
                },
            ],
            "authorization_boundary": (
                "Only a trusted audit LOGIN receives SELECT on this relation. The "
                "ordinary Identity API consumer keeps its existing five-function, "
                "two-view allowlist."
            ),
            "transaction_boundary": (
                "The caller owns the read-only transaction and snapshot; this relation "
                "does not commit, filter, or authorize rows."
            ),
        },
    }
    return schema


__all__ = [
    "ROLE_AUDIT_PROTOCOL",
    "ROLE_AUDIT_SCHEMA",
    "RoleAuditAccount",
    "RoleAuditInvitation",
    "RoleAuditSchemaCapabilities",
    "ROLE_AUDIT_VIEWS",
    "role_audit_schema",
]
