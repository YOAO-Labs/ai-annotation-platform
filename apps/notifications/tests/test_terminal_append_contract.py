from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_notifications.terminal_append import NotificationTerminalAppendManager
from aap_notifications.terminal_append_contract import (
    TERMINAL_APPEND_PROTOCOL,
    NotificationTerminalAppendUnavailable,
    TerminalNotificationRequest,
    decode_terminal_append_reply,
    terminal_append_json_schema,
)
from aap_notifications.terminal_append_postgres import (
    NotificationTerminalAppendPostgresData,
)

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_notifications"
_API = Path(__file__).resolve().parents[2] / "api"


def test_terminal_append_schema_and_migration_artifacts_match():
    schema = _PACKAGE / "terminal_append.schema.json"
    migration_schema = (
        _API / "alembic" / "sql" / "0321_notifications_terminal_append.schema.json"
    )
    package_sql = _PACKAGE / "sql" / "0321_notifications_terminal_append.sql"
    migration_sql = _API / "alembic" / "sql" / "0321_notifications_terminal_append.sql"

    assert schema.read_bytes() == migration_schema.read_bytes()
    assert json.loads(schema.read_text()) == terminal_append_json_schema()
    assert package_sql.read_bytes() == migration_sql.read_bytes()
    sql = package_sql.read_text()
    assert (
        "CREATE FUNCTION aap_notifications_v1.append_job_terminal_notification" in sql
    )
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "aap-dataset-import-notification:" in sql
    assert "pg_catalog.hashtextextended" in sql
    assert "current_setting('transaction_isolation')" in sql
    assert "unsupported_isolation" in sql
    assert "async_jobs" not in sql
    assert "read_actor_facts" not in sql
    assert "16384" not in sql


def _request(*, status="completed", payload=None):
    return TerminalNotificationRequest(
        schema=TERMINAL_APPEND_PROTOCOL,
        operation="append_job_terminal",
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type=f"job.{status}",
        target_id=uuid.uuid4(),
        payload=payload or {"kind": "batch_predict", "status": status},
    )


def test_terminal_request_is_closed_and_allows_projected_long_scope():
    request = _request(payload={"status": "completed", "scope": "x" * 20_000})
    dumped = json.loads(request.model_dump_json(by_alias=True))
    assert dumped["schema"] == TERMINAL_APPEND_PROTOCOL
    assert dumped["payload"]["scope"] == "x" * 20_000

    with pytest.raises(ValidationError):
        TerminalNotificationRequest.model_validate(
            {**request.model_dump(by_alias=True), "unexpected": True}
        )
    with pytest.raises(ValidationError, match="status must match"):
        _request(status="failed", payload={"status": "completed"})
    with pytest.raises(ValidationError, match="finite JSON values"):
        _request(payload={"status": "completed", "nested": float("nan")})


def _reply(request, *, created=True, notification_id=None, payload=None, read_at=None):
    return {
        "schema": TERMINAL_APPEND_PROTOCOL,
        "ok": True,
        "data": {
            "operation": "append_job_terminal",
            "created": created,
            "notification": {
                "id": str(notification_id or request.id),
                "user_id": str(request.user_id),
                "type": request.type,
                "target_type": "async_job",
                "target_id": str(request.target_id),
                "payload": payload if payload is not None else request.payload,
                "read_at": read_at,
                "created_at": datetime(2026, 10, 24, tzinfo=timezone.utc).isoformat(),
            },
        },
    }


def test_terminal_reply_requires_created_snapshot_binding_but_allows_existing_row():
    request = _request()
    created = decode_terminal_append_reply(_reply(request))
    assert created.data.created is True
    assert created.data.notification.id == request.id

    existing_id = uuid.uuid4()
    existing = decode_terminal_append_reply(
        _reply(
            request,
            created=False,
            notification_id=existing_id,
            payload={"status": "completed", "stored": "prior attempt"},
            read_at=datetime(2026, 10, 24, tzinfo=timezone.utc).isoformat(),
        )
    )
    assert existing.data.created is False
    assert existing.data.notification.id == existing_id

    with pytest.raises(NotificationTerminalAppendUnavailable):
        decode_terminal_append_reply(
            {
                "schema": TERMINAL_APPEND_PROTOCOL,
                "ok": False,
                "error": {"code": "unsupported_isolation"},
            }
        )


@pytest.mark.asyncio
async def test_terminal_manager_validates_owner_reply_and_postgres_binding():
    request = _request()

    class Data:
        def __init__(self, reply):
            self.reply = reply

        async def command(self, _request):
            return self.reply

    created = await NotificationTerminalAppendManager(Data(_reply(request))).append(
        request
    )
    assert created.created is True

    existing_reply = _reply(
        request,
        created=False,
        notification_id=uuid.uuid4(),
        payload={"status": "completed", "prior": True},
    )
    existing = await NotificationTerminalAppendManager(Data(existing_reply)).append(
        request
    )
    assert existing.created is False

    wrong_user = _reply(request)
    wrong_user["data"]["notification"]["user_id"] = str(uuid.uuid4())
    with pytest.raises(NotificationTerminalAppendUnavailable):
        await NotificationTerminalAppendManager(Data(wrong_user)).append(request)

    class Session:
        def __init__(self):
            self.calls = []

        async def scalar(self, statement, parameters=None):
            self.calls.append((str(statement), parameters))
            return _reply(request)

    session = Session()
    await NotificationTerminalAppendPostgresData(session).command(request)
    statement, parameters = session.calls[0]
    assert "append_job_terminal_notification" in statement
    assert "CAST(:request AS pg_catalog.jsonb)" in statement
    assert parameters["request"] == request.model_dump_json(by_alias=True)
