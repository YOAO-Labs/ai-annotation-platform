"""Portable contract for the REST Notifications inbox query capability."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter

INBOX_QUERY_PROTOCOL = "aap-notifications-inbox-query/v1"
INBOX_QUERY_FUNCTIONS = {
    "aap_notifications_v1.inbox_page": (
        "uuid, text, boolean, integer, integer",
        "SETOF aap_notifications_v1.notification_facts",
    ),
    "aap_notifications_v1.inbox_total_count": (
        "uuid, text",
        "bigint",
    ),
    "aap_notifications_v1.inbox_unread_count": (
        "uuid, text",
        "bigint",
    ),
}
INBOX_GLOBAL_ASYNC_JOB_KINDS = ("audit_archive", "dataset_import")


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NotificationInboxFact(_ClosedModel):
    """Native Notifications row; JSONB payload shapes pass through unchanged."""

    id: uuid.UUID
    user_id: uuid.UUID
    type: StrictStr
    target_type: StrictStr
    target_id: uuid.UUID
    payload: Any
    read_at: datetime | None
    created_at: datetime


def notification_inbox_query_schema() -> dict[str, Any]:
    schema = TypeAdapter(NotificationInboxFact).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": INBOX_QUERY_PROTOCOL,
            "title": "AAP Notifications REST inbox query",
            "$comment": (
                "The API passes the authenticated actor ID and platform role from "
                "its immediately preceding Identity actor-status snapshot. The "
                "capability owns inbox visibility, page filtering, ordering, and "
                "counts; calls borrow the caller's transaction."
            ),
            "x-postgresql-functions": {
                name: {"arguments": arguments, "returns": returns}
                for name, (arguments, returns) in INBOX_QUERY_FUNCTIONS.items()
            },
            "x-row-columns": list(NotificationInboxFact.model_fields),
            "x-row-key": ["id"],
            "x-payload": {
                "postgresql_type": "jsonb",
                "preserve_native_json_shapes": True,
            },
            "x-bigint-wire": "Keep PostgreSQL bigint counts as decimal text in Node clients.",
            "x-trusted-role-snapshot": "IdentityAccountLockData.read_actor_status",
            "x-trusted-actor-id": "Authenticated API actor ID checked by Identity preflight",
            "x-activity-policy": (
                "No endpoint-wide active-account filter; membership access still "
                "requires an active Identity profile"
            ),
            "x-global-async-job-kinds": list(INBOX_GLOBAL_ASYNC_JOB_KINDS),
            "x-security": {
                "functions": "security_definer",
                "search_path": ["pg_catalog", "pg_temp"],
                "public_execute": False,
                "inbox_query_client_execute": "three public inbox functions only",
                "private_helper_execute": False,
            },
            "x-private-helper": "aap_notifications_v1._visible_inbox_rows(uuid, text)",
            "x-borrowed-transaction": {
                "commit": False,
                "rollback": False,
                "autoflush": "preserve caller setting at every ORM query boundary",
            },
        }
    )
    return schema
