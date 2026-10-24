from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_source.import_delivery_contract import (
    DatasetImportDelivery,
    DatasetImportDeliveryCompletion,
)
from aap_notifications.publication_contract import PublicationAuthorization
from app.services import dataset_import_delivery as delivery_data
from app.services import notification as notification_module
from app.services.notification import NotificationService
from app.workers import dataset_import_delivery as delivery_worker


def test_delivery_wire_models_match_the_frozen_json_schema():
    schema_path = (
        Path(__file__).parents[1]
        / "alembic/sql/0214_dataset_import_delivery.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-postgresql-functions"] == [
        "aap_assets_v1.queue_dataset_import_delivery(uuid, text, uuid)",
        "aap_assets_v1.dataset_import_delivery_target_is_current(text, uuid)",
        "aap_assets_v1.claim_dataset_import_delivery(uuid, integer)",
        "aap_assets_v1.complete_dataset_import_delivery(uuid, uuid)",
    ]
    assert (
        schema["$defs"]["DatasetImportDelivery"]
        == DatasetImportDelivery.model_json_schema()
    )
    assert schema["$defs"]["DatasetImportDeliveryCompletion"] == (
        DatasetImportDeliveryCompletion.model_json_schema()
    )


def _delivery(kind="thumbnail"):
    return DatasetImportDelivery(
        delivery_id=uuid4(),
        job_id=uuid4(),
        kind=kind,
        target_id=uuid4(),
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc),
    )


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Session:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.calls = []

    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return _Result(self.next_value)

    async def commit(self):
        self.events.append("commit")


async def test_sql_adapter_decodes_claim_and_acknowledges_exact_lease():
    delivery = _delivery()
    db = _Session()
    db.next_value = {
        "schema": "aap-dataset-import-delivery/v1",
        "ok": True,
        "data": delivery.model_dump(mode="json"),
    }
    claimed = await delivery_data.claim_dataset_import_delivery(
        db, lease_token=delivery.lease_token, lease_seconds=120
    )
    assert claimed == delivery
    assert "claim_dataset_import_delivery" in db.calls[0][0]
    assert db.calls[0][1] == {
        "lease_token": delivery.lease_token,
        "lease_seconds": 120,
    }

    db.next_value = True
    assert await delivery_data.dataset_import_delivery_target_is_current(db, delivery)
    assert "dataset_import_delivery_target_is_current" in db.calls[1][0]

    db.next_value = {
        "schema": "aap-dataset-import-delivery/v1",
        "ok": True,
        "data": {
            "delivery_id": str(delivery.delivery_id),
            "lease_token": str(delivery.lease_token),
            "delivered": True,
        },
    }
    receipt = await delivery_data.complete_dataset_import_delivery(
        db,
        delivery_id=delivery.delivery_id,
        lease_token=delivery.lease_token,
    )
    assert receipt.delivery_id == delivery.delivery_id
    assert "complete_dataset_import_delivery" in db.calls[2][0]


async def test_media_delivery_is_acked_only_after_broker_accepts(monkeypatch):
    events = []
    delivery = _delivery("thumbnail")

    async def target_is_current(db, claimed):
        events.append("source_check")
        assert claimed == delivery
        return True

    async def complete(db, *, delivery_id, lease_token):
        events.append("ack")
        assert (delivery_id, lease_token) == (
            delivery.delivery_id,
            delivery.lease_token,
        )

    monkeypatch.setattr(
        delivery_worker, "dataset_import_delivery_target_is_current", target_is_current
    )
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", complete)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=lambda item_id: events.append(("publish", item_id)),
        dispatch_video_metadata=lambda item_id: events.append(
            ("publish_video", item_id)
        ),
    )
    db = _Session(events)

    assert await delivery_worker._deliver_one(db, delivery, dispatcher) == "delivered"
    assert events == [
        "source_check",
        "commit",
        ("publish", delivery.target_id),
        "ack",
        "commit",
    ]


async def test_missing_media_target_is_acked_without_recreation(monkeypatch):
    events = []
    delivery = _delivery("video_metadata")

    async def target_is_current(db, claimed):
        events.append("source_check")
        return False

    async def complete(db, *, delivery_id, lease_token):
        events.append("ack")

    monkeypatch.setattr(
        delivery_worker, "dataset_import_delivery_target_is_current", target_is_current
    )
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", complete)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=lambda _: events.append("publish"),
        dispatch_video_metadata=lambda _: events.append("publish"),
    )

    assert (
        await delivery_worker._deliver_one(_Session(events), delivery, dispatcher)
        == "stale"
    )
    assert events == ["source_check", "commit", "ack", "commit"]


@pytest.mark.parametrize("malformed", [None, "true", 1])
async def test_malformed_source_check_is_not_treated_as_stale_ack(
    monkeypatch, malformed
):
    delivery = _delivery("thumbnail")
    db = _Session()
    db.next_value = malformed
    ack = AsyncMock()
    monkeypatch.setattr(
        delivery_worker,
        "dataset_import_delivery_target_is_current",
        delivery_data.dataset_import_delivery_target_is_current,
    )
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", ack)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=AsyncMock(),
        dispatch_video_metadata=AsyncMock(),
    )

    with pytest.raises(ValueError, match="must be a boolean"):
        await delivery_worker._deliver_one(db, delivery, dispatcher)
    ack.assert_not_awaited()
    dispatcher.dispatch_thumbnail.assert_not_awaited()


async def test_broker_failure_leaves_delivery_unacknowledged(monkeypatch):
    delivery = _delivery("thumbnail")
    ack = AsyncMock()
    monkeypatch.setattr(
        delivery_worker,
        "dataset_import_delivery_target_is_current",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", ack)

    def publish(_item_id):
        raise RuntimeError("broker unavailable")

    dispatcher = SimpleNamespace(
        dispatch_thumbnail=publish,
        dispatch_video_metadata=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="broker unavailable"):
        await delivery_worker._deliver_one(_Session(), delivery, dispatcher)
    ack.assert_not_awaited()


async def test_notification_delivery_acks_after_publish_by_persisted_id(monkeypatch):
    events = []
    delivery = _delivery("notification")

    async def publish_by_id(self, notification_id):
        events.append(("publish_notification", notification_id))
        return True

    async def complete(db, *, delivery_id, lease_token):
        events.append("ack")
        assert (delivery_id, lease_token) == (
            delivery.delivery_id,
            delivery.lease_token,
        )

    monkeypatch.setattr(NotificationService, "publish_by_id", publish_by_id)
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", complete)
    db = _Session(events)

    assert (
        await delivery_worker._deliver_one(db, delivery, SimpleNamespace())
        == "delivered"
    )
    assert events == [
        ("publish_notification", delivery.target_id),
        "commit",
        "ack",
        "commit",
    ]


async def test_notification_transport_failure_leaves_delivery_unacknowledged(
    monkeypatch,
):
    delivery = _delivery("notification")
    ack = AsyncMock()

    async def publish_by_id(self, _notification_id):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(NotificationService, "publish_by_id", publish_by_id)
    monkeypatch.setattr(delivery_worker, "complete_dataset_import_delivery", ack)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await delivery_worker._deliver_one(_Session(), delivery, SimpleNamespace())
    ack.assert_not_awaited()


async def test_notification_publish_failure_propagates_for_retry(monkeypatch):
    row = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        type="dataset_import_completed",
        target_type="async_job",
        target_id=uuid4(),
        payload={},
        created_at=None,
    )
    service = NotificationService(SimpleNamespace())
    monkeypatch.setattr(service, "notification_by_id", AsyncMock(return_value=row))
    service._resolve_delivery_scopes = AsyncMock(return_value=[(False, None, False)])
    service._publication_authorizations = AsyncMock(
        return_value=[
            PublicationAuthorization(
                restricted=False,
                project_id=None,
                export_required=False,
                member=False,
                reviewer=False,
                owner=False,
                super_admin=False,
            )
        ]
    )
    monkeypatch.setattr(
        notification_module,
        "_publish",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await service.publish_by_id(row.id)


async def test_missing_notification_is_a_safe_ack(monkeypatch):
    db = SimpleNamespace()
    service = NotificationService(db)
    monkeypatch.setattr(service, "notification_by_id", AsyncMock(return_value=None))
    publish = AsyncMock()
    monkeypatch.setattr(notification_module, "_publish", publish)

    assert await service.publish_by_id(uuid4()) is False
    publish.assert_not_awaited()


async def test_revoked_notification_is_suppressed_and_ackable(monkeypatch):
    row = SimpleNamespace(id=uuid4(), user_id=uuid4(), type="dataset_import_completed")
    service = NotificationService(SimpleNamespace())
    monkeypatch.setattr(service, "notification_by_id", AsyncMock(return_value=row))
    service._resolve_delivery_scopes = AsyncMock(return_value=[(True, uuid4(), False)])
    service._publication_authorizations = AsyncMock(
        return_value=[
            PublicationAuthorization(
                restricted=True,
                project_id=uuid4(),
                export_required=False,
                member=False,
                reviewer=False,
                owner=False,
                super_admin=False,
            )
        ]
    )
    publish = AsyncMock()
    monkeypatch.setattr(notification_module, "_publish", publish)

    assert await service.publish_by_id(row.id) is False
    publish.assert_not_awaited()
