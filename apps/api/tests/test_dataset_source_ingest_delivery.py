"""Operation-scoped ZIP/scan media delivery and bounded relay fairness."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_source.ingest_contract import (
    INGEST_DELIVERY_SCHEMA,
    IngestDeliveryLease,
    IngestFailure,
)
from app.services import dataset_import_delivery as target_data
from app.services import dataset_source_ingest_delivery as data
from app.workers import dataset_import_delivery as worker


def _lease(token=None):
    return IngestDeliveryLease(
        id=uuid4(),
        operation_id=uuid4(),
        kind="thumbnail",
        target_id=uuid4(),
        lease_token=token or uuid4(),
        lease_expires_at=datetime.now(timezone.utc),
    )


class _Session:
    def __init__(self, value=None, events=None):
        self.value = value
        self.events = events if events is not None else []

    async def execute(self, statement, parameters):
        return SimpleNamespace(scalar_one=lambda: self.value)

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


async def test_claim_and_ack_are_bound_to_the_exact_lease():
    lease = _lease()
    db = _Session(
        {
            "schema": INGEST_DELIVERY_SCHEMA,
            "ok": True,
            "data": lease.model_dump(mode="json"),
        }
    )
    assert (
        await data.claim_dataset_source_ingest_delivery(
            db, lease_token=lease.lease_token, lease_seconds=120
        )
        == lease
    )
    with pytest.raises(IngestFailure, match="invalid_response"):
        await data.claim_dataset_source_ingest_delivery(
            db, lease_token=uuid4(), lease_seconds=120
        )
    db.value = {
        "schema": INGEST_DELIVERY_SCHEMA,
        "ok": True,
        "data": {
            "id": str(lease.id),
            "lease_token": str(lease.lease_token),
            "delivered": True,
        },
    }
    assert (
        await data.complete_dataset_source_ingest_delivery(
            db, delivery_id=lease.id, lease_token=lease.lease_token
        )
    ).delivered is True
    with pytest.raises(IngestFailure, match="invalid_response"):
        await data.complete_dataset_source_ingest_delivery(
            db, delivery_id=lease.id, lease_token=uuid4()
        )


async def test_media_ack_follows_source_check_and_broker_acceptance(monkeypatch):
    lease, events = _lease(), []
    monkeypatch.setattr(
        worker,
        "dataset_import_delivery_target_is_current",
        AsyncMock(return_value=True),
    )

    async def ack(_db, **scope):
        assert scope == {"delivery_id": lease.id, "lease_token": lease.lease_token}
        events.append("ack")

    monkeypatch.setattr(worker, "complete_dataset_source_ingest_delivery", ack)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=lambda item: events.append(("publish", item))
    )
    assert (
        await worker._deliver_source_ingest_one(
            _Session(events=events), lease, dispatcher
        )
        == "delivered"
    )
    assert events == ["commit", ("publish", lease.target_id), "ack", "commit"]


@pytest.mark.parametrize("target_reply", [None, "false", 1])
async def test_bad_target_reply_does_not_ack_or_publish(monkeypatch, target_reply):
    lease = _lease()
    monkeypatch.setattr(
        worker,
        "dataset_import_delivery_target_is_current",
        target_data.dataset_import_delivery_target_is_current,
    )
    ack = AsyncMock()
    monkeypatch.setattr(worker, "complete_dataset_source_ingest_delivery", ack)
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=lambda _: pytest.fail("unexpected publication")
    )
    with pytest.raises(ValueError, match="must be a boolean"):
        await worker._deliver_source_ingest_one(
            _Session(target_reply), lease, dispatcher
        )
    ack.assert_not_awaited()


async def test_broker_failure_keeps_operation_delivery_pending(monkeypatch):
    monkeypatch.setattr(
        worker,
        "dataset_import_delivery_target_is_current",
        AsyncMock(return_value=True),
    )
    ack = AsyncMock()
    monkeypatch.setattr(worker, "complete_dataset_source_ingest_delivery", ack)

    def publish(_):
        raise RuntimeError("broker unavailable")

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await worker._deliver_source_ingest_one(
            _Session(), _lease(), SimpleNamespace(dispatch_thumbnail=publish)
        )
    ack.assert_not_awaited()


@pytest.mark.parametrize("legacy_busy", [True, False])
async def test_both_queues_share_a_bounded_budget_without_starvation(
    monkeypatch, legacy_busy
):
    seen = []

    @asynccontextmanager
    async def session():
        yield _Session()

    async def connector_claim(_db, *, lease_token, lease_seconds):
        return _lease(lease_token) if legacy_busy else None

    async def ingest_claim(_db, *, lease_token, lease_seconds):
        return _lease(lease_token)

    async def connector_deliver(*_):
        seen.append("connector")
        return "delivered"

    async def ingest_deliver(*_):
        seen.append("ingest")
        return "delivered"

    monkeypatch.setattr(worker, "task_session", session)
    monkeypatch.setattr(worker, "CeleryMediaJobDispatcher", SimpleNamespace)
    monkeypatch.setattr(worker, "claim_dataset_import_delivery", connector_claim)
    monkeypatch.setattr(worker, "claim_dataset_source_ingest_delivery", ingest_claim)
    monkeypatch.setattr(worker, "_deliver_one", connector_deliver)
    monkeypatch.setattr(worker, "_deliver_source_ingest_one", ingest_deliver)
    totals = await worker._relay_pending()
    assert totals == {"claimed": 100, "delivered": 100, "stale": 0, "deferred": 0}
    assert seen == (["connector", "ingest"] * 50 if legacy_busy else ["ingest"] * 100)
