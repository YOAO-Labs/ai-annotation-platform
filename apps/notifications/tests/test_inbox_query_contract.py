from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest

from aap_notifications.inbox_query_contract import NotificationInboxFact


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], ["project_id"], "project_id", 5, True],
)
def test_inbox_row_contract_preserves_native_jsonb_payload(payload):
    actor_id = uuid.uuid4()
    row = NotificationInboxFact(
        id=uuid.uuid4(),
        user_id=actor_id,
        type="inbox.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=payload,
        read_at=None,
        created_at=datetime.now(timezone.utc),
    )

    assert row.payload == payload
