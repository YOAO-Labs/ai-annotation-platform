"""Portable read contract for the historical project-role audit's Workspace facts."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictStr, TypeAdapter

from aap_workspace_contract.wire import ClosedModel

ROLE_AUDIT_PROTOCOL = "aap-workspace-role-audit/v1"
ROLE_AUDIT_SCHEMA = "aap_workspace_audit_v1"
ROLE_AUDIT_VIEWS = (
    "project_facts",
    "project_member_facts",
    "schema_capabilities",
)

RoleText = Annotated[StrictStr, Field(max_length=30)]


class WorkspaceRoleAuditProject(ClosedModel):
    """Project facts used by the audit, preserving raw nullable diagnostics."""

    id: uuid.UUID
    owner_id: uuid.UUID | None
    status: RoleText | None


class WorkspaceRoleAuditProjectMember(ClosedModel):
    """Membership facts, without joining or normalizing Identity roles."""

    id: uuid.UUID
    project_id: uuid.UUID | None
    user_id: uuid.UUID | None
    role: RoleText | None


class WorkspaceRoleAuditSchemaCapabilities(ClosedModel):
    """Presence of the two ProjectMember fields added in revision 0173."""

    member_version: StrictBool
    member_updated_at: StrictBool


_ROW_MODELS = (
    WorkspaceRoleAuditProject,
    WorkspaceRoleAuditProjectMember,
    WorkspaceRoleAuditSchemaCapabilities,
)


def _relation_columns(
    model: type[ClosedModel],
    postgres_types: tuple[str, ...],
    nullability: tuple[bool, ...],
) -> list[dict[str, Any]]:
    fields = TypeAdapter(model).json_schema()["properties"]
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


def workspace_role_audit_schema() -> dict[str, Any]:
    """Describe the fixed, unfiltered Workspace-owned audit relations."""
    definitions = {
        model.__name__: TypeAdapter(model).json_schema() for model in _ROW_MODELS
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ROLE_AUDIT_PROTOCOL,
        "$defs": definitions,
        "title": "Workspace project-role audit relation rows",
        "oneOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in _ROW_MODELS],
        "x-postgres-read-relations": {
            "protocol": ROLE_AUDIT_PROTOCOL,
            "relations": [
                {
                    "name": "project_facts",
                    "qualified_name": f"{ROLE_AUDIT_SCHEMA}.project_facts",
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        WorkspaceRoleAuditProject,
                        ("uuid", "uuid", "character varying(30)"),
                        (False, True, True),
                    ),
                    "semantics": (
                        "One row for every public.projects row, without filtering, "
                        "joining, ordering, or deduplication. Only id, owner_id, and "
                        "status are exposed; owner and status values remain raw, "
                        "including NULL where present. The caller retains the "
                        "historical LEFT JOIN behavior and owns the read-only "
                        "transaction snapshot."
                    ),
                },
                {
                    "name": "project_member_facts",
                    "qualified_name": (f"{ROLE_AUDIT_SCHEMA}.project_member_facts"),
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        WorkspaceRoleAuditProjectMember,
                        (
                            "uuid",
                            "uuid",
                            "uuid",
                            "character varying(30)",
                        ),
                        (False, True, True, True),
                    ),
                    "semantics": (
                        "One row for every public.project_members row, without "
                        "filtering, Identity joins, role mapping, ordering, or "
                        "deduplication. Unknown role strings and NULL diagnostics "
                        "remain raw. The caller performs the historical joins to "
                        "account_roles inside its SQL and owns the read-only "
                        "transaction snapshot."
                    ),
                },
                {
                    "name": "schema_capabilities",
                    "qualified_name": f"{ROLE_AUDIT_SCHEMA}.schema_capabilities",
                    "kind": "security_barrier_view",
                    "columns": _relation_columns(
                        WorkspaceRoleAuditSchemaCapabilities,
                        ("boolean", "boolean"),
                        (False, False),
                    ),
                    "semantics": (
                        "Exactly one row reports whether "
                        "public.project_members.version and .updated_at exist. Values come from "
                        "pg_catalog.pg_attribute, so additive revision 0173 is "
                        "visible without reinstalling these fixed views."
                    ),
                },
            ],
            "authorization_boundary": (
                "Only a trusted historical-audit LOGIN receives SELECT on these "
                "relations. The ordinary Workspace API consumer keeps its existing "
                "fixed allowlist."
            ),
            "transaction_boundary": (
                "The caller owns the read-only transaction and snapshot; these "
                "relations do not commit, filter, or authorize rows."
            ),
        },
    }


__all__ = [
    "ROLE_AUDIT_PROTOCOL",
    "ROLE_AUDIT_SCHEMA",
    "ROLE_AUDIT_VIEWS",
    "WorkspaceRoleAuditProject",
    "WorkspaceRoleAuditProjectMember",
    "WorkspaceRoleAuditSchemaCapabilities",
    "workspace_role_audit_schema",
]
