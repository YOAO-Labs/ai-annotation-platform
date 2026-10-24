from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aap_notifications.notification_delivery_contract import NotificationRequestedLease
from aap_notifications.delivery_worker import (
    NotificationPublisher,
    _notification_message,
)


WORKER_ID = uuid.UUID("60000000-0000-4000-8000-000000000001")


def _lease() -> NotificationRequestedLease:
    now = datetime.now(UTC)
    return NotificationRequestedLease(
        id=uuid.UUID("60000000-0000-4000-8000-000000000002"),
        user_id=uuid.UUID("60000000-0000-4000-8000-000000000003"),
        type="user.deactivation_requested",
        target_type="user",
        target_id=uuid.UUID("60000000-0000-4000-8000-000000000004"),
        payload={
            "email": "person@example.test",
            "name": "Person",
            "role": "employee",
            "scheduled_at": now + timedelta(days=7),
        },
        created_at=now,
        worker_id=WORKER_ID,
        lease_id=uuid.UUID("60000000-0000-4000-8000-000000000005"),
        lease_expires_at=now + timedelta(seconds=60),
    )


class FakeData:
    def __init__(self, lease, *, accepted=True):
        self.lease = lease
        self.accepted = accepted
        self.claimed = False
        self.finished = []

    async def claim_deliveries(self, worker_id, *, limit, lease_seconds):
        assert worker_id == WORKER_ID
        assert limit == 16
        assert lease_seconds == 60
        self.claimed = True
        return [] if self.lease is None else [self.lease]

    async def finish_delivery(self, lease, *, outcome, reason):
        self.finished.append((lease, outcome, reason))
        return self.accepted


class FakeRedis:
    def __init__(self, data, *, result=0, error=None):
        self.data = data
        self.result = result
        self.error = error
        self.calls = []

    async def publish(self, channel, message):
        assert self.data.claimed
        self.calls.append((channel, message))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_publisher_emits_existing_websocket_shape_then_finishes_stable_id():
    lease = _lease()
    data = FakeData(lease)
    redis = FakeRedis(data, result=0)

    result = await NotificationPublisher(data, redis, worker_id=WORKER_ID).run_once()

    channel, encoded = redis.calls[0]
    message = json.loads(encoded)
    expected = {
        "id": str(lease.id),
        "type": lease.type,
        "target_type": lease.target_type,
        "target_id": str(lease.target_id),
        "payload": lease.payload.model_dump(mode="json", by_alias=True),
        "created_at": lease.created_at.astimezone(UTC).isoformat(),
    }
    assert channel == f"notify:{lease.user_id}"
    assert encoded == json.dumps(
        expected,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert set(message) == {
        "id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "created_at",
    }
    assert message["id"] == str(lease.id)
    assert message["target_type"] == "user"
    assert message["target_id"] == str(lease.target_id)
    assert "user_id" not in message
    assert message["payload"]["email"] == "person@example.test"
    assert data.finished == [(lease, "delivered", "published")]
    assert result == {
        "claimed": 1,
        "published": 1,
        "retry": 0,
        "stale": 0,
        "unavailable": 0,
    }


@pytest.mark.asyncio
async def test_publish_failure_retries_with_only_safe_reason():
    lease = _lease()
    data = FakeData(lease)
    redis = FakeRedis(data, error=RuntimeError("redis://user:password@cache"))

    result = await NotificationPublisher(data, redis, worker_id=WORKER_ID).run_once()

    assert data.finished == [(lease, "retry", "redis_error")]
    assert result == {
        "claimed": 1,
        "published": 0,
        "retry": 1,
        "stale": 0,
        "unavailable": 0,
    }
    assert "password" not in repr(result)


@pytest.mark.asyncio
async def test_lost_finish_fence_reports_stale_after_redis_accepts_message():
    lease = _lease()
    data = FakeData(lease, accepted=False)
    redis = FakeRedis(data)

    result = await NotificationPublisher(data, redis, worker_id=WORKER_ID).run_once()

    assert len(redis.calls) == 1
    assert data.finished == [(lease, "delivered", "published")]
    assert result["published"] == 0
    assert result["stale"] == 1


def test_notification_message_is_safe_and_uses_existing_channel_shape():
    lease = _lease()
    channel, encoded = _notification_message(lease)
    assert channel == f"notify:{lease.user_id}"
    assert set(json.loads(encoded)) == {
        "id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "created_at",
    }


def test_publisher_enforces_contract_bounds():
    with pytest.raises(ValueError):
        NotificationPublisher(object(), object(), concurrency=5)
    with pytest.raises(ValueError):
        NotificationPublisher(object(), object(), redis_timeout_seconds=5.1)
