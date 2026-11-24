from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete

from yoao_assets_source.import_delivery_contract import DatasetImportDelivery
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from app.services import dataset_import_delivery as delivery_data
from app.services import notification as notification_module
from app.services.notification import NotificationService
from app.workers import dataset_import_delivery as delivery_worker
from tests.factory import create_membership, create_project


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

    async def execute(self, statement, parameters):
        return _Result(self.next_value)

    async def commit(self):
        self.events.append("commit")


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


async def test_notification_publish_failure_propagates_for_retry(
    db_session, annotator, monkeypatch
):
    user, _ = annotator
    job = AsyncJob(
        id=uuid4(),
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    row = Notification(
        user_id=user.id,
        type="dataset_import_completed",
        target_type="async_job",
        target_id=job.id,
        payload={},
    )
    db_session.add_all([job, row])
    await db_session.flush()
    service = NotificationService(db_session)
    monkeypatch.setattr(
        notification_module.settings, "notification_publication_url", ""
    )
    publish = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(notification_module, "_publish", publish)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await service.publish_by_id(row.id)
    publish.assert_awaited_once()


async def test_missing_notification_is_a_safe_ack(db_session, monkeypatch):
    service = NotificationService(db_session)
    publish = AsyncMock()
    monkeypatch.setattr(notification_module, "_publish", publish)

    assert await service.publish_by_id(uuid4()) is False
    publish.assert_not_awaited()


async def test_revoked_notification_is_suppressed_and_ackable(
    db_session, super_admin, annotator, monkeypatch
):
    owner, _ = super_admin
    user, _ = annotator
    project = await create_project(db_session, owner_id=owner.id)
    membership = await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="reviewer"
    )
    job = AsyncJob(
        id=uuid4(),
        kind="batch_predict",
        project_id=project.id,
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    row = Notification(
        user_id=user.id,
        type="job.completed",
        target_type="async_job",
        target_id=job.id,
        payload={},
    )
    db_session.add_all([job, row])
    await db_session.flush()
    service = NotificationService(db_session)
    monkeypatch.setattr(
        notification_module.settings, "notification_publication_url", ""
    )
    publish = AsyncMock()
    monkeypatch.setattr(notification_module, "_publish", publish)

    assert await service.publish_by_id(row.id) is True
    publish.assert_awaited_once()
    await db_session.execute(
        delete(ProjectMember).where(ProjectMember.id == membership.id)
    )
    assert await service.publish_by_id(row.id) is False
    publish.assert_awaited_once()
