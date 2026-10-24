from __future__ import annotations

from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models.notification import Notification
from app.services.identity_contracts import IdentityActorStatus
from app.services import notification as notification_module
from app.services.async_job_access import GLOBAL_ASYNC_JOB_KINDS
from aap_notifications.inbox_query_contract import INBOX_GLOBAL_ASYNC_JOB_KINDS
from aap_notifications.inbox_query_postgres import NotificationInboxQueryPostgresData


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return _Scalars(self.rows)


class _QuerySession:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append(("page", statement, params))
        return _Result([self.row])

    async def scalar(self, statement, params=None):
        self.calls.append(("count", statement, params))
        return "7" if "inbox_total_count" in str(statement) else 4


def test_inbox_global_job_kind_policy_matches_existing_jobs_contract():
    assert frozenset(INBOX_GLOBAL_ASYNC_JOB_KINDS) == GLOBAL_ASYNC_JOB_KINDS


@pytest.mark.asyncio
async def test_postgres_adapter_calls_only_fixed_page_and_count_functions():
    actor_id = uuid.uuid4()
    row = Notification(
        id=uuid.uuid4(),
        user_id=actor_id,
        type="inbox.test",
        target_type="task",
        target_id=uuid.uuid4(),
        payload=["native", "jsonb"],
    )
    session = _QuerySession(row)
    inbox = NotificationInboxQueryPostgresData(session, Notification)

    page = await inbox.page(
        actor_id,
        "project_admin",
        unread_only=True,
        limit=8,
        offset=3,
    )
    total = await inbox.total_count(actor_id, "project_admin")
    unread = await inbox.unread_count(actor_id, "project_admin")

    assert page == [row]
    assert page[0] is row
    assert (total, unread) == (7, 4)
    assert [kind for kind, _statement, _params in session.calls] == [
        "page",
        "count",
        "count",
    ]
    page_sql = str(session.calls[0][1].compile(dialect=postgresql.dialect()))
    total_sql = str(session.calls[1][1].compile(dialect=postgresql.dialect()))
    unread_sql = str(session.calls[2][1].compile(dialect=postgresql.dialect()))
    assert "aap_notifications_v1.inbox_page" in page_sql
    assert "aap_notifications_v1.inbox_total_count" in total_sql
    assert "aap_notifications_v1.inbox_unread_count" in unread_sql
    assert session.calls[0][2] == {
        "actor_id": actor_id,
        "platform_role": "project_admin",
        "unread_only": True,
        "limit": 8,
        "offset": 3,
    }


@pytest.mark.asyncio
async def test_rest_inbox_preserves_identity_snapshot_and_query_order(monkeypatch):
    actor_id = uuid.uuid4()
    actor = IdentityActorStatus(
        id=actor_id,
        role="project_admin",
        is_active=False,
    )
    row = object()
    events = []
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    class _Identity:
        def __init__(self, session):
            assert session is db

        async def read_actor_status(self, requested_actor_id):
            events.append(("status", requested_actor_id))
            return actor

    class _Inbox:
        def __init__(self, session, model):
            assert session is db
            assert model is Notification

        async def page(self, requested_actor_id, role, **kwargs):
            events.append(("page", requested_actor_id, role, kwargs))
            return [row]

        async def total_count(self, requested_actor_id, role):
            events.append(("total", requested_actor_id, role))
            return 9

        async def unread_count(self, requested_actor_id, role):
            events.append(("unread", requested_actor_id, role))
            return 6

    monkeypatch.setattr(notification_module, "IdentityAccountLockData", _Identity)
    monkeypatch.setattr(
        notification_module, "NotificationInboxQueryPostgresData", _Inbox
    )
    service = notification_module.NotificationService(db)

    assert await service.list_for_user(
        actor_id,
        unread_only=True,
        limit=8,
        offset=3,
    ) == ([row], 9, 6)
    assert events == [
        ("status", actor_id),
        (
            "page",
            actor_id,
            "project_admin",
            {"unread_only": True, "limit": 8, "offset": 3},
        ),
        ("total", actor_id, "project_admin"),
        ("unread", actor_id, "project_admin"),
    ]
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_unread_count_keeps_status_then_single_owner_call(monkeypatch):
    actor_id = uuid.uuid4()
    actor = IdentityActorStatus(id=actor_id, role="viewer", is_active=True)
    events = []

    class _Identity:
        def __init__(self, session):
            pass

        async def read_actor_status(self, requested_actor_id):
            events.append(("status", requested_actor_id))
            return actor

    class _Inbox:
        def __init__(self, session, model):
            pass

        async def unread_count(self, requested_actor_id, role):
            events.append(("unread", requested_actor_id, role))
            return 3

    monkeypatch.setattr(notification_module, "IdentityAccountLockData", _Identity)
    monkeypatch.setattr(
        notification_module, "NotificationInboxQueryPostgresData", _Inbox
    )

    assert (
        await notification_module.NotificationService(object()).unread_count(actor_id)
        == 3
    )
    assert events == [
        ("status", actor_id),
        ("unread", actor_id, "viewer"),
    ]


@pytest.mark.asyncio
async def test_missing_actor_skips_all_inbox_capabilities(monkeypatch):
    actor_id = uuid.uuid4()
    events = []

    class _Identity:
        def __init__(self, session):
            pass

        async def read_actor_status(self, requested_actor_id):
            events.append(("status", requested_actor_id))
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
    assert events == [("status", actor_id), ("status", actor_id)]
