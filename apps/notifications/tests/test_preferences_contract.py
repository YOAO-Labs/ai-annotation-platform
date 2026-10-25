from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_notifications.preferences_contract import (
    GetPreferencesRequest,
    IsInAppMutedRequest,
    PatchPreferenceRequest,
    NotificationPreferencesUnavailable,
    decode_get_preferences_reply,
    preferences_json_schema,
)
from aap_notifications.preferences_policy import (
    DEFAULT_TOAST_TYPES,
    KNOWN_NOTIFICATION_TYPES,
)
from aap_notifications.preferences_postgres import NotificationPreferencesPostgresData

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_notifications"


def test_wire_schema_is_one_closed_document_with_all_local_references():
    package_schema = _PACKAGE / "preferences.schema.json"
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0311_notifications_preferences.schema.json"
    )
    assert package_schema.read_bytes() == migration_schema.read_bytes()
    schema = json.loads(package_schema.read_text())
    assert schema == preferences_json_schema()
    assert schema["$defs"]

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


def test_policy_json_matches_the_owner_sql_policy():
    policy = json.loads((_PACKAGE / "preferences.policy.json").read_text())
    assert tuple(policy["known_notification_types"]) == KNOWN_NOTIFICATION_TYPES
    assert frozenset(policy["default_toast_types"]) == DEFAULT_TOAST_TYPES

    sql = (_PACKAGE / "sql" / "0311_notifications_preferences.sql").read_text()
    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "alembic"
        / "sql"
        / "0311_notifications_preferences.sql"
    )
    assert sql == migration_sql.read_text()
    body = sql.split("CREATE FUNCTION aap_notifications_v1._preference_policy()", 1)[1]
    body = body.split("AS $function$", 1)[1].split("$function$", 1)[0]
    rows = re.findall(
        r"\(\s*(\d+)(?:::\w+)?\s*,\s*'([^']+)'(?:::\w+)?\s*,\s*(true|false)\s*\)",
        body,
    )
    assert rows == [
        (
            str(index),
            notification_type,
            str(notification_type in DEFAULT_TOAST_TYPES).lower(),
        )
        for index, notification_type in enumerate(KNOWN_NOTIFICATION_TYPES, start=1)
    ]


def test_patch_request_has_no_client_supplied_defaults_and_mute_accepts_future_types():
    user_id = uuid.uuid4()
    request = PatchPreferenceRequest(
        schema="aap-notification-preferences/v1",
        operation="patch",
        user_id=user_id,
        type="task.rejected",
        patch={"in_app": False},
    )
    assert set(json.loads(request.model_dump_json(by_alias=True))) == {
        "schema",
        "operation",
        "user_id",
        "type",
        "patch",
    }

    mute = IsInAppMutedRequest(
        schema="aap-notification-preferences/v1",
        operation="is_in_app_muted",
        user_id=user_id,
        type="future.notification",
    )
    assert mute.type == "future.notification"
    with pytest.raises(ValidationError):
        PatchPreferenceRequest(
            schema="aap-notification-preferences/v1",
            operation="patch",
            user_id=user_id,
            type="task.rejected",
            patch={},
        )


def test_reply_decoder_rejects_foreign_or_malformed_owner_results():
    with pytest.raises(NotificationPreferencesUnavailable):
        decode_get_preferences_reply(
            {
                "schema": "wrong/v1",
                "ok": True,
                "data": {"operation": "get", "items": []},
            }
        )
    with pytest.raises(NotificationPreferencesUnavailable):
        decode_get_preferences_reply(
            {
                "schema": "aap-notification-preferences/v1",
                "ok": True,
                "data": {"operation": "get", "items": [{"type": "task.rejected"}]},
            }
        )


class _FakeSession:
    def __init__(self, *, autoflush: bool):
        self.autoflush = autoflush
        self.flushes = 0
        self.calls = []

    async def flush(self):
        self.flushes += 1

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        return {"ok": True}


@pytest.mark.asyncio
async def test_postgres_binding_flushes_like_orm_reads_without_committing():
    request = GetPreferencesRequest(
        schema="aap-notification-preferences/v1",
        operation="get",
        user_id=uuid.uuid4(),
    )
    normal = _FakeSession(autoflush=True)
    await NotificationPreferencesPostgresData(normal).command(request)
    assert normal.flushes == 1
    assert "aap_notifications_v1.preference_command" in normal.calls[0][0]
    assert json.loads(normal.calls[0][1]["request"])["operation"] == "get"

    no_autoflush = _FakeSession(autoflush=False)
    await NotificationPreferencesPostgresData(no_autoflush).command(request)
    assert no_autoflush.flushes == 0
