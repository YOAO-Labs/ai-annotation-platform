"""Closed schema for Notifications' public read-only row projection."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter

READ_RELATIONS_PROTOCOL = "aap-notifications-read-relations/v1"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NotificationFact(_ClosedModel):
    id: uuid.UUID
    user_id: uuid.UUID
    type: StrictStr
    target_type: StrictStr
    target_id: uuid.UUID
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


def notification_read_relation_schema() -> dict:
    schema = TypeAdapter(NotificationFact).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": READ_RELATIONS_PROTOCOL,
            "title": "AAP Notifications read relations",
            "$comment": (
                "The fixed read-only projection publishes Notification row facts. "
                "Consumers retain their existing authorization, filtering, counts, "
                "ordering, pagination and borrowed transaction semantics."
            ),
            "x-postgresql-relations": {
                "aap_notifications_v1.notification_facts": {
                    "columns": list(NotificationFact.model_fields),
                    "row_schema": "#",
                    "row_key": ["id"],
                    "read_only": True,
                    "security_barrier": True,
                }
            },
        }
    )
    return schema
