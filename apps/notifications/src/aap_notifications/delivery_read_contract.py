"""Portable contract for Notifications' current-delivery reads and policy facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter

DELIVERY_READ_PROTOCOL = "aap-notifications-delivery-read/v1"
DELIVERY_READ_GLOBAL_JOB_KINDS = ("audit_archive", "dataset_import")
DELIVERY_READ_FUNCTIONS = {
    "aap_notifications_v1.read_notification": {
        "arguments": "uuid",
        "returns": "SETOF aap_notifications_v1.notification_facts",
    },
    "aap_notifications_v1.find_terminal_notification": {
        "arguments": "uuid, text, uuid",
        "returns": "uuid or null",
    },
    "aap_notifications_v1.delivery_job_scopes": {
        "arguments": "uuid[]",
        "returns": "TABLE(id uuid, scope_project_id uuid, kind varchar(40))",
    },
    "aap_notifications_v1.delivery_member_pairs": {
        "arguments": "jsonb pair array",
        "returns": "TABLE(project_id uuid, user_id uuid)",
    },
    "aap_notifications_v1.delivery_reviewer_pairs": {
        "arguments": "jsonb pair array",
        "returns": "TABLE(project_id uuid, user_id uuid)",
    },
    "aap_notifications_v1.delivery_owner_pairs": {
        "arguments": "jsonb pair array",
        "returns": "TABLE(project_id uuid, user_id uuid)",
    },
    "aap_notifications_v1.delivery_super_pairs": {
        "arguments": "jsonb pair array",
        "returns": "TABLE(project_id uuid, user_id uuid)",
    },
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NotificationDeliveryReadFact(_ClosedModel):
    """Native notification row; payload retains every PostgreSQL JSONB shape."""

    id: uuid.UUID
    user_id: uuid.UUID
    type: StrictStr
    target_type: StrictStr
    target_id: uuid.UUID
    payload: Any
    read_at: datetime | None
    created_at: datetime


class DeliveryReadPair(_ClosedModel):
    project_id: uuid.UUID
    user_id: uuid.UUID


def notification_delivery_read_schema() -> dict[str, Any]:
    schema = TypeAdapter(NotificationDeliveryReadFact).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": DELIVERY_READ_PROTOCOL,
            "title": "AAP Notifications delivery reads",
            "$comment": (
                "Fixed read capabilities borrow the API caller's transaction. "
                "They do not commit, lock, or authorize the WebSocket credential."
            ),
            "x-postgresql-functions": DELIVERY_READ_FUNCTIONS,
            "x-row-columns": list(NotificationDeliveryReadFact.model_fields),
            "x-row-key": ["id"],
            "x-payload": {
                "postgresql_type": "jsonb",
                "preserve_native_json_shapes": True,
            },
            "x-pair-input": {
                "schema": TypeAdapter(list[DeliveryReadPair]).json_schema(),
                "exact_keys": ["project_id", "user_id"],
                "uuid_strings": "canonical lower-case hyphenated UUIDs",
                "preserve_pairs": True,
                "cardinality_cap": None,
            },
            "x-policy": {
                "global_async_job_kinds": list(DELIVERY_READ_GLOBAL_JOB_KINDS),
                "actual_job_scope_wins_over_payload": True,
                "other_target_scope": (
                    "Python dict payload only; key presence restricts even when null"
                ),
                "project_id_parser": "uuid.UUID(str(value))",
                "fail_closed": [
                    "missing job",
                    "unknown global job kind",
                    "invalid project UUID",
                ],
                "membership": "active current platform role, known project role, viewer pairing",
                "owner": "active manager account owns project",
                "super_admin": "active super-admin account and existing project",
            },
            "x-error-fallback": {
                "after_scope_read_failure": (
                    "Allow only rows whose target is not export/async_job and whose "
                    "dict payload has no project_id key; non-dict payload is {}"
                )
            },
            "x-policy-query-order": {
                "resolve_scopes": "one delivery_job_scopes call only when job IDs exist",
                "allowed_pairs": [
                    "delivery_member_pairs",
                    "delivery_reviewer_pairs",
                    "delivery_owner_pairs",
                    "delivery_super_pairs",
                ],
                "allowed_pair_reads": "four distinct ordered calls only for nonempty pairs",
            },
            "x-security": {
                "functions": "stable security_definer",
                "search_path": ["pg_catalog", "pg_temp"],
                "public_execute": False,
                "foreign_private_tables": False,
                "invalid_pair_sqlstate": "22023",
            },
            "x-borrowed-transaction": {"commit": False, "rollback": False},
        }
    )
    return schema
