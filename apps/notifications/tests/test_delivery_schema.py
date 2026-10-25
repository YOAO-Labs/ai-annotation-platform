from __future__ import annotations

import json
from pathlib import Path

from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_PROTOCOL,
    notification_delivery_protocol_schema,
)


def test_delivery_schema_is_owned_by_notifications_and_keeps_identity_copy():
    root = Path(__file__).resolve().parents[3]
    notifications_schema = (
        root
        / "apps/notifications/src/aap_notifications/notification_delivery_protocol.schema.json"
    ).read_bytes()
    identity_compat_schema = (
        root
        / "apps/identity/src/aap_identity/notification_delivery_protocol.schema.json"
    ).read_bytes()
    schema = json.loads(notifications_schema)

    assert schema == notification_delivery_protocol_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == NOTIFICATION_DELIVERY_PROTOCOL
    assert identity_compat_schema == notifications_schema
    assert set(schema["$defs"]) >= {
        "NotificationClaimRequest",
        "NotificationClaimReply",
        "NotificationFinishRequest",
        "NotificationFinishReply",
    }
