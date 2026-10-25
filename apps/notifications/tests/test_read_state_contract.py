from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_notifications.read_state import NotificationReadStateManager
from aap_notifications.read_state_contract import (
    MarkAllReadRequest,
    MarkReadRequest,
    NotificationReadStateUnavailable,
    decode_read_state_reply,
    read_state_json_schema,
)
from aap_notifications.read_state_postgres import NotificationReadStatePostgresData

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_notifications"


def test_wire_schema_is_one_closed_document_with_local_refs():
    package_schema = _PACKAGE / "read_state.schema.json"
    package_sql = _PACKAGE / "sql" / "0316_notifications_read_state.sql"
    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0316_notifications_read_state.sql"
    )
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0316_notifications_read_state.schema.json"
    )
    assert package_schema.read_bytes() == migration_schema.read_bytes()
    assert package_sql.read_bytes() == migration_sql.read_bytes()
    schema = json.loads(package_schema.read_text())
    assert schema == read_state_json_schema()
    assert schema["$id"] == "aap-notification-read-state/v1"

    def check_refs(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                assert reference.startswith("#/$defs/")
                assert reference.removeprefix("#/$defs/") in schema["$defs"]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)


def test_requests_are_closed_and_use_one_utc_timestamp():
    user_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    read_at = datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc)
    request = MarkReadRequest(
        schema="aap-notification-read-state/v1",
        operation="mark_read",
        user_id=user_id,
        notification_id=notification_id,
        read_at=read_at,
    )
    assert json.loads(request.model_dump_json(by_alias=True)) == {
        "schema": "aap-notification-read-state/v1",
        "operation": "mark_read",
        "user_id": str(user_id),
        "notification_id": str(notification_id),
        "read_at": "2026-10-23T12:30:00Z",
    }
    with pytest.raises(ValidationError):
        MarkAllReadRequest(
            schema="aap-notification-read-state/v1",
            operation="mark_all_read",
            user_id=user_id,
            read_at=datetime(2026, 10, 23, 20, 30, tzinfo=timezone(timedelta(hours=8))),
        )
    with pytest.raises(ValidationError):
        MarkAllReadRequest(
            schema="aap-notification-read-state/v1",
            operation="mark_all_read",
            user_id=user_id,
            read_at=datetime(2026, 10, 23, 12, 30),
        )


def test_reply_decoder_checks_count_ids_and_rejects_bad_owner_replies():
    user_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    reply = decode_read_state_reply(
        {
            "schema": "aap-notification-read-state/v1",
            "ok": True,
            "data": {
                "operation": "mark_all_read",
                "updated": 1,
                "updated_ids": [str(notification_id)],
            },
        }
    )
    assert reply.data.updated_ids == [notification_id]

    with pytest.raises(ValidationError):
        MarkAllReadRequest(
            schema="aap-notification-read-state/v1",
            operation="mark_all_read",
            user_id=user_id,
            read_at=datetime.now(timezone.utc),
            unexpected=True,
        )
    with pytest.raises(NotificationReadStateUnavailable):
        decode_read_state_reply(
            {
                "schema": "aap-notification-read-state/v1",
                "ok": True,
                "data": {
                    "operation": "mark_all_read",
                    "updated": 2,
                    "updated_ids": [str(notification_id)],
                },
            }
        )
    with pytest.raises(NotificationReadStateUnavailable):
        decode_read_state_reply(
            {
                "schema": "aap-notification-read-state/v1",
                "ok": True,
                "data": {
                    "operation": "mark_read",
                    "updated": False,
                    "updated_ids": [str(notification_id), str(uuid.uuid4())],
                },
            }
        )
    with pytest.raises(NotificationReadStateUnavailable):
        decode_read_state_reply(
            {
                "schema": "aap-notification-read-state/v1",
                "ok": True,
                "data": {
                    "operation": "mark_read",
                    "updated": True,
                    "updated_ids": [],
                },
            }
        )


class _FakeSession:
    def __init__(self, *, autoflush: bool, reply):
        self.autoflush = autoflush
        self.reply = reply
        self.flushes = 0
        self.calls = []

    async def flush(self):
        self.flushes += 1

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        return self.reply


@pytest.mark.asyncio
async def test_postgres_binding_preserves_flush_modes_and_does_not_commit():
    user_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    read_at = datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc)
    reply = {
        "schema": "aap-notification-read-state/v1",
        "ok": True,
        "data": {
            "operation": "mark_read",
            "updated": True,
            "updated_ids": [str(notification_id)],
        },
    }
    normal = _FakeSession(autoflush=True, reply=reply)
    data = NotificationReadStatePostgresData(normal)
    result = await NotificationReadStateManager(data).mark_read(
        user_id, notification_id, read_at
    )
    assert result.updated is True
    assert normal.flushes == 1
    statement, parameters = normal.calls[0]
    assert "aap_notifications_v1.read_state_command" in statement
    assert "FROM public.notifications" not in statement
    assert json.loads(parameters["request"]) == {
        "schema": "aap-notification-read-state/v1",
        "operation": "mark_read",
        "user_id": str(user_id),
        "notification_id": str(notification_id),
        "read_at": "2026-10-23T12:30:00Z",
    }

    no_autoflush = _FakeSession(autoflush=False, reply=reply)
    await NotificationReadStatePostgresData(no_autoflush).command(
        MarkReadRequest(
            schema="aap-notification-read-state/v1",
            operation="mark_read",
            user_id=user_id,
            notification_id=notification_id,
            read_at=read_at,
        )
    )
    assert no_autoflush.flushes == 0

    mismatched = _FakeSession(
        autoflush=False,
        reply={
            "schema": "aap-notification-read-state/v1",
            "ok": True,
            "data": {
                "operation": "mark_read",
                "updated": True,
                "updated_ids": [str(uuid.uuid4())],
            },
        },
    )
    with pytest.raises(NotificationReadStateUnavailable):
        await NotificationReadStateManager(
            NotificationReadStatePostgresData(mismatched)
        ).mark_read(user_id, notification_id, read_at)


@pytest.mark.asyncio
async def test_mark_all_manager_returns_owner_count_and_ids():
    user_id = uuid.uuid4()
    notification_ids = [uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession(
        autoflush=False,
        reply={
            "schema": "aap-notification-read-state/v1",
            "ok": True,
            "data": {
                "operation": "mark_all_read",
                "updated": len(notification_ids),
                "updated_ids": [str(value) for value in notification_ids],
            },
        },
    )
    result = await NotificationReadStateManager(
        NotificationReadStatePostgresData(session)
    ).mark_all_read(user_id, datetime.now(timezone.utc))
    assert result.updated == 2
    assert result.updated_ids == notification_ids
    assert session.flushes == 0
