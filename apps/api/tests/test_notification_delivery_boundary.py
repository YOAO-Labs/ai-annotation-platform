"""The embedded Notifications publisher never spans database locks across Redis."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import uuid

import pytest

from aap_notifications.delivery_worker import NotificationPublisher
from aap_notifications.notification_delivery_contract import (
    NotificationDeliveryUnavailable,
)
from app.config import settings
from app.services import notification_delivery
from app.services.identity_contracts import IdentityGatewayUnavailable


def claim(worker_id):
    return {
        "schema": "aap-notifications-delivery/v1",
        "ok": True,
        "data": {
            "items": [
                {
                    "id": str(uuid.uuid4()),
                    "user_id": str(uuid.uuid4()),
                    "type": "user.deactivation_completed",
                    "target_type": "user",
                    "target_id": str(uuid.uuid4()),
                    "payload": {
                        "email": "leaving@delivery.test",
                        "name": "Leaving",
                        "auto": True,
                    },
                    "created_at": datetime.now(UTC).isoformat(),
                    "worker_id": str(worker_id),
                    "lease_id": str(uuid.uuid4()),
                    "lease_expires_at": (
                        datetime.now(UTC) + timedelta(seconds=60)
                    ).isoformat(),
                }
            ]
        },
    }


class Sessions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.open = self.commits = self.rollbacks = 0
        self.commands = []

    @asynccontextmanager
    async def __call__(self):
        self.open += 1

        async def scalar(statement, parameters):
            self.commands.append((str(statement), parameters))
            return next(self.responses)

        async def commit():
            self.commits += 1

        async def rollback():
            self.rollbacks += 1

        try:
            yield SimpleNamespace(scalar=scalar, commit=commit, rollback=rollback)
        finally:
            self.open -= 1


async def test_claim_is_committed_and_closed_before_redis_publish():
    worker_id = uuid.uuid4()
    raw = claim(worker_id)
    item = raw["data"]["items"][0]
    sessions = Sessions(
        [
            raw,
            {
                "schema": "aap-notifications-delivery/v1",
                "ok": True,
                "data": {"accepted": True},
            },
        ]
    )
    seen = []

    class Redis:
        async def publish(self, channel, message):
            assert sessions.commits == 1 and sessions.open == 0
            seen.append((channel, json.loads(message)))
            return 0

    result = await NotificationPublisher(
        notification_delivery.NotificationDeliveryData(sessions),
        Redis(),
        worker_id=worker_id,
    ).run_once()
    assert result == {
        "claimed": 1,
        "published": 1,
        "retry": 0,
        "stale": 0,
        "unavailable": 0,
    }
    assert sessions.commits == 2 and sessions.rollbacks == 0 and sessions.open == 0
    assert seen[0][0] == f"notify:{item['user_id']}"
    assert seen[0][1] == {
        key: item[key]
        for key in ("id", "type", "target_type", "target_id", "payload", "created_at")
    }
    assert (
        json.loads(sessions.commands[1][1]["command"])["notification_id"] == item["id"]
    )


@pytest.mark.parametrize("fault", ["worker", "duplicate", "secret", "integer_boolean"])
async def test_invalid_claim_rolls_back_before_commit_or_publication(fault):
    worker_id = uuid.uuid4()
    raw = claim(worker_id)
    item = raw["data"]["items"][0]
    if fault == "worker":
        item["worker_id"] = str(uuid.uuid4())
    elif fault == "duplicate":
        raw["data"]["items"].append(item.copy())
    elif fault == "secret":
        item["payload"]["reason"] = "must-not-publish"
    else:
        item["payload"]["auto"] = 1
    sessions = Sessions([raw])
    with pytest.raises(NotificationDeliveryUnavailable):
        await notification_delivery.NotificationDeliveryData(sessions).claim_deliveries(
            worker_id
        )
    assert sessions.commits == 0 and sessions.rollbacks == 1 and sessions.open == 0


async def test_invalid_finish_rolls_back_after_redis_without_acknowledging_delivery():
    worker_id = uuid.uuid4()
    sessions = Sessions(
        [
            claim(worker_id),
            {
                "schema": "aap-notifications-delivery/v1",
                "ok": True,
                "data": {"accepted": 1},
            },
        ]
    )

    class Redis:
        async def publish(self, *args):
            assert sessions.open == 0
            return 1

    result = await NotificationPublisher(
        notification_delivery.NotificationDeliveryData(sessions),
        Redis(),
        worker_id=worker_id,
    ).run_once()
    assert result["unavailable"] == 1 and result["published"] == 0
    assert sessions.commits == 1 and sessions.rollbacks == 1


async def test_remote_identity_without_publisher_binding_selects_external_publisher(
    monkeypatch,
):
    monkeypatch.setattr(settings, "notification_publish_database_url", "")
    monkeypatch.setattr(settings, "notification_publish_database_url_file", "")
    monkeypatch.setattr(
        notification_delivery,
        "get_identity_gateway",
        lambda: SimpleNamespace(identity_url="http://identity.test"),
    )

    def forbidden(*args):
        raise AssertionError("An external publisher must not select caller SQL")

    monkeypatch.setattr(notification_delivery, "NotificationDeliveryData", forbidden)
    assert await notification_delivery.run_identity_notifications() == {
        "state": "remote"
    }


async def test_explicit_publisher_binding_failure_never_selects_caller_sql(monkeypatch):
    monkeypatch.setattr(settings, "notification_publish_database_url", "")
    monkeypatch.setattr(
        settings, "notification_publish_database_url_file", "/missing-publisher-binding"
    )

    def forbidden(*args):
        raise AssertionError("Invalid explicit publisher binding must fail closed")

    monkeypatch.setattr(notification_delivery, "NotificationDeliveryData", forbidden)
    with pytest.raises(IdentityGatewayUnavailable):
        await notification_delivery.run_identity_notifications(redis=object())


async def test_explicit_publisher_binding_can_run_alongside_remote_identity(
    monkeypatch,
):
    monkeypatch.setattr(
        settings,
        "notification_publish_database_url",
        "postgresql://publisher.invalid/fixture",
    )
    monkeypatch.setattr(settings, "notification_publish_database_url_file", "")
    monkeypatch.setattr(
        notification_delivery,
        "get_identity_gateway",
        lambda: SimpleNamespace(identity_url="http://identity.test"),
    )

    class Data:
        def __init__(self, dsn):
            assert dsn == settings.notification_publish_database_url

        async def delivery_policy(self):
            return SimpleNamespace(policy="aap-notifications-delivery/v1")

        async def claim_deliveries(self, worker_id, **kwargs):
            return []

    monkeypatch.setattr(notification_delivery, "NotificationDeliveryPostgresData", Data)
    result = await notification_delivery.run_identity_notifications(redis=object())
    assert result["state"] == "idle" and result["claimed"] == 0
