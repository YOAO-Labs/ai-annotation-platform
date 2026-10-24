from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

from fastapi.encoders import jsonable_encoder
import pytest

from app.schemas.notification import NotificationList, NotificationOut


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": [{"count": 2**80, "label": "雪"}]},
        ["legacy", None, 2**100, False],
        "legacy scalar",
        0,
        False,
        None,
    ],
)
def test_notification_response_serialization_preserves_native_json_payload(payload):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        type="legacy.notification",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=payload,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    response = NotificationList(
        items=[NotificationOut.model_validate(row)], total=1, unread=1
    )
    encoded = json.dumps(jsonable_encoder(response), ensure_ascii=False)
    decoded = json.loads(encoded)

    assert decoded["items"][0]["payload"] == payload


def test_notification_response_keeps_empty_object_default():
    notification = NotificationOut(
        id=uuid.uuid4(),
        type="new.notification",
        target_type="task",
        target_id=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
    )

    assert notification.payload == {}
