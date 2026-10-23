"""Public, read-only Identity relation values for transactional SQL consumers."""

from __future__ import annotations

import uuid

from pydantic import Field, TypeAdapter

from .contract import ClosedModel

READ_RELATION_PROTOCOL = "aap-identity-read-relations/v1"


class AccountProfile(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    is_active: bool = Field(strict=True)
    status: str = Field(strict=True)
    avatar_ref: str | None


class InvitationAuditTarget(ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID | None


def identity_read_relation_schema() -> dict:
    schema = TypeAdapter(AccountProfile | InvitationAuditTarget).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": READ_RELATION_PROTOCOL,
            "$comment": (
                "Fixed read-only owner relations, not authorization decisions. "
                "Trusted consumers retain their existing scope predicates, "
                "joins, ordering and transaction snapshot. No credential, "
                "generation or lifecycle fields are published."
            ),
            "x-postgresql-relations": {
                "aap_identity_v1.account_profiles": {
                    "columns": list(AccountProfile.model_fields),
                    "row": {"$ref": "#/$defs/AccountProfile"},
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                },
                "aap_identity_v1.invitation_audit_targets": {
                    "columns": list(InvitationAuditTarget.model_fields),
                    "row": {"$ref": "#/$defs/InvitationAuditTarget"},
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                },
            },
        }
    )
    return schema
