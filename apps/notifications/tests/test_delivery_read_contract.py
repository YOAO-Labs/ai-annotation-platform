from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from pydantic import ValidationError

from yoao_notifications.delivery_read_contract import (
    DeliveryReadPair,
    NotificationDeliveryReadFact,
)


@pytest.mark.parametrize("payload", [None, {}, [], [1, "x"], "x", 7, True])
def test_delivery_fact_keeps_native_jsonb_payload_shapes(payload):
    fact = NotificationDeliveryReadFact(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="task.approved",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=payload,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    assert fact.payload == payload


def test_delivery_pair_contract_rejects_extra_fields():
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()

    assert DeliveryReadPair.model_validate(
        {"project_id": project_id, "user_id": user_id}
    ) == DeliveryReadPair(project_id=project_id, user_id=user_id)
    with pytest.raises(ValidationError):
        DeliveryReadPair.model_validate(
            {"project_id": project_id, "user_id": user_id, "extra": True}
        )
