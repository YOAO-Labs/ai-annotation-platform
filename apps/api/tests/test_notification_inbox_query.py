from __future__ import annotations

import uuid

import pytest

from app.services import notification as notification_module


@pytest.mark.asyncio
async def test_missing_actor_skips_all_inbox_capabilities(monkeypatch):
    actor_id = uuid.uuid4()

    class _Identity:
        def __init__(self, session):
            pass

        async def read_actor_status(self, requested_actor_id):
            return None

    class _Inbox:
        def __init__(self, session, model):
            pytest.fail("missing actors must not call inbox capabilities")

    monkeypatch.setattr(notification_module, "IdentityAccountLockData", _Identity)
    monkeypatch.setattr(
        notification_module, "NotificationInboxQueryPostgresData", _Inbox
    )

    service = notification_module.NotificationService(object())
    assert await service.list_for_user(actor_id) == ([], 0, 0)
    assert await service.unread_count(actor_id) == 0
