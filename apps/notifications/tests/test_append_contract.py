from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_notifications.append_contract import (
    AppendNotificationRequest,
    NotificationAppendUnavailable,
    append_json_schema,
    decode_append_reply,
)
from aap_notifications.append import NotificationAppendManager
from aap_notifications.append_postgres import NotificationAppendPostgresData


_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_notifications"


def test_append_wire_schema_and_migration_artifact_match():
    package_schema = _PACKAGE / "append.schema.json"
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0314_notifications_append.schema.json"
    )
    assert package_schema.read_bytes() == migration_schema.read_bytes()
    schema = json.loads(package_schema.read_text())
    assert schema == append_json_schema()
    assert schema["$id"] == "aap-notification-append/v1"
    assert schema["$defs"]["AppendNotificationRequest"]["additionalProperties"] is False
    assert "payload" in schema["$defs"]["AppendNotificationRequest"]["required"]

    sql = (_PACKAGE / "sql" / "0314_notifications_append.sql").read_text()
    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0314_notifications_append.sql"
    )
    assert sql == migration_sql.read_text()
    assert "SECURITY DEFINER" in sql
    assert "clock_timestamp" not in sql
    assert "AT TIME ZONE 'UTC'" in sql
    assert "is_active" not in sql


def test_request_is_closed_and_preserves_caller_generated_identity():
    request = AppendNotificationRequest(
        schema="aap-notification-append/v1",
        operation="append",
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="future.notification",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"empty": [], "false": False},
    )
    dumped = json.loads(request.model_dump_json(by_alias=True))
    assert dumped["id"] == str(request.id)
    assert dumped["payload"] == {"empty": [], "false": False}
    python_dump = request.model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        AppendNotificationRequest.model_validate({**python_dump, "unexpected": True})
    with pytest.raises(ValidationError):
        AppendNotificationRequest.model_validate(
            {key: value for key, value in python_dump.items() if key != "payload"}
        )


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), uuid.uuid4()]
)
def test_request_rejects_payload_values_the_jsonb_writer_could_not_store(value):
    with pytest.raises(ValidationError, match="finite JSON values"):
        AppendNotificationRequest(
            schema="aap-notification-append/v1",
            operation="append",
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            type="future.notification",
            target_type="task",
            target_id=uuid.uuid4(),
            payload={"nested": [value]},
        )


def test_reply_decoder_parses_server_timestamp_and_requires_notification_snapshot():
    notification_id = uuid.uuid4()
    user_id = uuid.uuid4()
    target_id = uuid.uuid4()
    timestamp = datetime(2026, 10, 23, 2, 15, tzinfo=timezone.utc)
    decoded = decode_append_reply(
        {
            "schema": "aap-notification-append/v1",
            "ok": True,
            "data": {
                "operation": "append",
                "notification": {
                    "id": str(notification_id),
                    "user_id": str(user_id),
                    "type": "task.approved",
                    "target_type": "task",
                    "target_id": str(target_id),
                    "payload": {"task_id": str(target_id)},
                    "read_at": None,
                    "created_at": timestamp.isoformat(),
                },
            },
        }
    )
    assert decoded.data.notification is not None
    assert decoded.data.notification.id == notification_id
    assert decoded.data.notification.created_at == timestamp

    invalid_timestamp = {
        "schema": "aap-notification-append/v1",
        "ok": True,
        "data": {
            "operation": "append",
            "notification": {
                "id": str(notification_id),
                "user_id": str(user_id),
                "type": "task.approved",
                "target_type": "task",
                "target_id": str(target_id),
                "payload": {"task_id": str(target_id)},
                "read_at": None,
                "created_at": "2026-10-23T02:15:00",
            },
        },
    }
    with pytest.raises(NotificationAppendUnavailable):
        decode_append_reply(invalid_timestamp)


@pytest.mark.asyncio
async def test_append_manager_rejects_a_snapshot_for_another_request():
    request = AppendNotificationRequest(
        schema="aap-notification-append/v1",
        operation="append",
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="task.approved",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"value": 1},
    )

    class _ReplyData:
        async def command(self, _request):
            return {
                "schema": "aap-notification-append/v1",
                "ok": True,
                "data": {
                    "operation": "append",
                    "notification": {
                        "id": str(uuid.uuid4()),
                        "user_id": str(request.user_id),
                        "type": request.type,
                        "target_type": request.target_type,
                        "target_id": str(request.target_id),
                        "payload": request.payload,
                        "read_at": None,
                        "created_at": "2026-10-23T02:15:00Z",
                    },
                },
            }

    with pytest.raises(NotificationAppendUnavailable):
        await NotificationAppendManager(_ReplyData()).append(request)


def test_reply_decoder_rejects_foreign_protocol_and_invalid_owner_error():
    with pytest.raises(NotificationAppendUnavailable):
        decode_append_reply(
            {"schema": "wrong/v1", "ok": True, "data": {"operation": "append"}}
        )
    with pytest.raises(NotificationAppendUnavailable):
        decode_append_reply(
            {
                "schema": "aap-notification-append/v1",
                "ok": False,
                "error": {"code": "unexpected"},
            }
        )


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        return {"ok": True}


@pytest.mark.asyncio
async def test_postgres_binding_borrows_transaction_without_flush_or_commit():
    session = _FakeSession()
    request = AppendNotificationRequest(
        schema="aap-notification-append/v1",
        operation="append",
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type="task.approved",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={},
    )

    await NotificationAppendPostgresData(session).command(request)

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "aap_notifications_v1.append_notification" in statement
    assert json.loads(parameters["request"]) == json.loads(
        request.model_dump_json(by_alias=True, exclude_none=True)
    )
