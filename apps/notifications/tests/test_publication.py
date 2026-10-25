from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

from aap_notifications.publication import NotificationPublicationManager
from aap_notifications.publication_contract import (
    ImmediatePublicationUnavailable,
    PublicationAuthorization,
)


_PROJECT_UNSET = object()


def _authorization(
    *, restricted=True, project_id=_PROJECT_UNSET, export_required=False, member=False
):
    return PublicationAuthorization(
        restricted=restricted,
        project_id=uuid.uuid4() if project_id is _PROJECT_UNSET else project_id,
        export_required=export_required,
        member=member,
        reviewer=False,
        owner=False,
        super_admin=False,
    )


def _row(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "type": "future.notification.type",
        "target_type": "task",
        "target_id": uuid.uuid4(),
        "payload": {"unicode": "你好", "count": 2**90},
        "created_at": datetime(
            2026, 10, 24, 12, 30, tzinfo=timezone(timedelta(hours=8))
        ),
    }
    values.update(overrides)
    return type("NotificationRow", (), values)()


class FakeData:
    def __init__(self, rows=(), facts=(), *, error=None):
        self.rows = list(rows)
        self.facts = list(facts)
        self.error = error
        self.calls = []

    async def notification_by_id(self, notification_id):
        self.calls.append(("by_id", notification_id))
        return next((row for row in self.rows if row.id == notification_id), None)

    async def resolve_scopes(self, rows):
        self.calls.append(("scopes", list(rows)))
        if self.error is not None:
            raise self.error
        return [
            (fact.restricted, fact.project_id, fact.export_required)
            for fact in self.facts
        ]

    async def authorization_facts(self, rows, scopes):
        self.calls.append(("authorization", list(rows), list(scopes)))
        if self.error is not None:
            raise self.error
        return self.facts


@pytest.mark.asyncio
async def test_committed_manager_builds_after_authorization_and_preserves_event_values():
    user_id = uuid.uuid4()
    order = []

    class TrackedRow:
        id = uuid.uuid4()
        type = "future.notification.type"
        target_type = "task"
        target_id = uuid.uuid4()
        payload = ["native", {"count": 2**90}]
        created_at = datetime(2026, 10, 24, 12, 30, tzinfo=timezone(timedelta(hours=8)))

        def __getattribute__(self, name):
            if name in {
                "user_id",
                "id",
                "type",
                "target_type",
                "target_id",
                "payload",
                "created_at",
            }:
                order.append(name)
            return object.__getattribute__(self, name)

        @property
        def user_id(self):
            return user_id

    row = TrackedRow()

    class Data:
        async def resolve_scopes(self, rows):
            order.append("scopes")
            assert rows == [row]
            return [(False, None, False)]

        async def authorization_facts(self, rows, scopes):
            order.append("authorization")
            assert rows == [row]
            return [_authorization(restricted=False, project_id=None)]

    sent = []

    async def transport(request):
        sent.append(request)
        return True

    await NotificationPublicationManager(Data(), transport).publish_committed(
        iter([row])
    )

    assert order == [
        "scopes",
        "authorization",
        "user_id",
        "id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "created_at",
    ]
    assert len(sent) == 1
    assert sent[0].user_id == user_id
    assert sent[0].authorization.restricted is False
    assert sent[0].message == {
        "id": str(row.id),
        "type": row.type,
        "target_type": row.target_type,
        "target_id": str(row.target_id),
        "payload": row.payload,
        "created_at": "2026-10-24T12:30:00+08:00",
    }
    assert json.loads(json.dumps(sent[0].message))["payload"] == row.payload


@pytest.mark.asyncio
async def test_authorization_failure_fallback_only_builds_unrestricted_messages():
    unrestricted = _row(payload={"note": "ok"})
    denied = _row(target_type="async_job", payload={"unserializable": object()})
    data = FakeData(
        [unrestricted, denied], error=RuntimeError("private database detail")
    )
    sent = []

    async def transport(request):
        sent.append(request)
        return True

    await NotificationPublicationManager(data, transport).publish_committed(
        [unrestricted, denied]
    )

    assert len(sent) == 1
    assert sent[0].user_id == unrestricted.user_id
    assert sent[0].authorization == _authorization(
        restricted=False,
        project_id=None,
    )
    assert "private database detail" not in repr(sent)


@pytest.mark.asyncio
async def test_denied_invalid_payload_is_never_serialized():
    row = _row(target_type="export", payload={"project_id": None, "secret": object()})
    data = FakeData([row], [_authorization(project_id=None)])
    sent = []

    async def transport(request):
        sent.append(request)
        return True

    await NotificationPublicationManager(data, transport).publish_committed([row])

    assert sent == []


@pytest.mark.asyncio
async def test_committed_publication_continues_after_each_row_failure():
    rows = [_row(), _row(), _row()]
    data = FakeData(rows, [_authorization(member=True) for _ in rows])
    attempts = []

    async def transport(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise RuntimeError("must not leak")
        return len(attempts) == 3

    await NotificationPublicationManager(data, transport).publish_committed(rows)

    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_publish_by_id_returns_false_for_missing_or_denied_and_propagates_transport():
    missing = NotificationPublicationManager(FakeData(), lambda _request: None)
    assert await missing.publish_by_id(uuid.uuid4()) is False

    row = _row()
    denied = NotificationPublicationManager(
        FakeData([row], [_authorization(project_id=None)]), lambda _request: None
    )
    assert await denied.publish_by_id(row.id) is False

    permitted = NotificationPublicationManager(
        FakeData([row], [_authorization(member=True)]),
        lambda _request: _raise_unavailable(),
    )
    with pytest.raises(ImmediatePublicationUnavailable):
        await permitted.publish_by_id(row.id)


async def _raise_unavailable():
    raise ImmediatePublicationUnavailable()


@pytest.mark.asyncio
async def test_sync_is_best_effort_but_propagates_cancellation():
    async def failing(_request):
        raise RuntimeError("secret transport detail")

    await NotificationPublicationManager(FakeData(), failing).publish_sync(
        uuid.uuid4(), reason="read"
    )

    async def cancelled(_request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await NotificationPublicationManager(FakeData(), cancelled).publish_sync(
            uuid.uuid4(), reason="read"
        )
