from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import notification as notification_module
from app.services.notification import NotificationService

_DEFAULT_PAYLOAD = object()


def _row(*, created_at=None, payload=_DEFAULT_PAYLOAD):
    return SimpleNamespace(
        id=uuid.UUID("60000000-0000-4000-8000-000000000001"),
        user_id=uuid.UUID("60000000-0000-4000-8000-000000000002"),
        type="future.module_event",
        target_type="future_target",
        target_id=uuid.UUID("60000000-0000-4000-8000-000000000003"),
        payload={"text": "中文"} if payload is _DEFAULT_PAYLOAD else payload,
        created_at=created_at,
    )


def _service(row):
    service = object.__new__(NotificationService)

    async def resolve_delivery_scopes(rows):
        assert rows == [row]
        return [(False, None, False)]

    async def allowed_delivery_indices(rows, _scopes):
        assert rows == [row]
        return {0}

    async def notification_by_id(notification_id):
        assert notification_id == row.id
        return row

    service._resolve_delivery_scopes = resolve_delivery_scopes
    service._allowed_delivery_indices = allowed_delivery_indices
    service.notification_by_id = notification_by_id
    return service


@pytest.mark.asyncio
async def test_api_event_builder_keeps_fields_and_offset_timestamp(monkeypatch):
    created_at = datetime(
        2026, 10, 24, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    row = _row(created_at=created_at)
    service = _service(row)
    published = []

    async def capture(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr(notification_module, "_publish", capture)
    await service.publish_committed([row])

    assert published == [
        (
            row.user_id,
            {
                "id": str(row.id),
                "type": "future.module_event",
                "target_type": "future_target",
                "target_id": str(row.target_id),
                "payload": {"text": "中文"},
                "created_at": "2026-10-24T10:00:00+05:30",
            },
        )
    ]


@pytest.mark.asyncio
async def test_api_event_builder_keeps_utc_fallback(monkeypatch):
    row = _row(created_at=None)
    service = _service(row)
    published = []

    async def capture(*, user_id, message):
        published.append(message)

    monkeypatch.setattr(notification_module, "_publish", capture)
    before = datetime.now(timezone.utc)
    await service.publish_committed([row])
    after = datetime.now(timezone.utc)

    created_at = datetime.fromisoformat(published[0]["created_at"])
    assert before <= created_at <= after
    assert created_at.utcoffset() == timedelta(0)
    assert published[0]["created_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_best_effort_publish_still_swallows_errors_and_propagates_cancel(
    monkeypatch,
):
    row = _row(created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
    service = _service(row)

    attempts = []

    async def fail_publish(*, user_id, message):
        attempts.append((user_id, message))
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(notification_module, "_publish", fail_publish)
    await service.publish_committed([row])
    await service.publish_sync(row.user_id, reason="preferences")
    assert attempts == [
        (
            row.user_id,
            {
                "id": str(row.id),
                "type": row.type,
                "target_type": row.target_type,
                "target_id": str(row.target_id),
                "payload": row.payload,
                "created_at": "2026-10-24T00:00:00+00:00",
            },
        ),
        (
            row.user_id,
            {"type": "notifications.sync", "reason": "preferences"},
        ),
    ]

    async def cancel_publish(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(notification_module, "_publish", cancel_publish)
    with pytest.raises(asyncio.CancelledError):
        await service.publish_committed([row])
    with pytest.raises(asyncio.CancelledError):
        await service.publish_sync(row.user_id, reason="read")


@pytest.mark.asyncio
async def test_durable_publish_path_still_propagates_transport_errors(monkeypatch):
    row = _row(created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
    service = _service(row)

    attempts = []

    async def fail_publish(*, user_id, message):
        attempts.append((user_id, message))
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(notification_module, "_publish", fail_publish)
    with pytest.raises(RuntimeError, match="redis unavailable"):
        await service.publish_by_id(row.id)
    assert attempts == [
        (
            row.user_id,
            {
                "id": str(row.id),
                "type": row.type,
                "target_type": row.target_type,
                "target_id": str(row.target_id),
                "payload": row.payload,
                "created_at": "2026-10-24T00:00:00+00:00",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], ["legacy"], "legacy", 7, True])
async def test_api_publish_methods_preserve_native_jsonb_payload_shapes(
    monkeypatch, payload
):
    row = _row(
        created_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        payload=payload,
    )
    service = _service(row)
    messages = []

    async def capture(*, user_id, message):
        messages.append((user_id, message))

    monkeypatch.setattr(notification_module, "_publish", capture)
    await service.publish_committed([row])
    await service.publish_by_id(row.id)

    assert len(messages) == 2
    assert [message["payload"] for _user_id, message in messages] == [payload, payload]
