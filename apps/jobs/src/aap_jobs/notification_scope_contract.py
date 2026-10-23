"""Closed contract for Jobs' public notification scope relation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter

NOTIFICATION_SCOPE_PROTOCOL = "aap-jobs-notification-scope-facts/v1"
NOTIFICATION_SCOPE_RELATION = "aap_jobs_v1.notification_scope_facts"
NOTIFICATION_SCOPE_COLUMNS = ("id", "kind", "scope_project_id")


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NotificationScopeFact(_ClosedModel):
    """One authoritative Jobs identity/kind/project-scope fact."""

    id: UUID
    kind: StrictStr = Field(max_length=40)
    scope_project_id: UUID | None


def notification_scope_schema() -> dict[str, Any]:
    schema = TypeAdapter(NotificationScopeFact).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": NOTIFICATION_SCOPE_PROTOCOL,
            "title": "AAP Jobs notification scope facts",
            "$comment": (
                "This relation publishes only job identity, native kind, and "
                "retained project scope. Consumers own authorization policy."
            ),
            "x-postgresql-relations": {
                NOTIFICATION_SCOPE_RELATION: {
                    "columns": list(NOTIFICATION_SCOPE_COLUMNS),
                    "column_types": {
                        "id": "uuid",
                        "kind": "character varying(40)",
                        "scope_project_id": "uuid",
                    },
                    "nullable_columns": ["scope_project_id"],
                    "row_schema": "#",
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                }
            },
        }
    )
    return schema


def notification_scope_sql() -> str:
    from importlib.resources import files

    return (
        files("aap_jobs")
        .joinpath("sql/notification_scope_facts.sql")
        .read_text(encoding="utf-8")
    )
