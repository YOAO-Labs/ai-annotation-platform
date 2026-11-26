from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from yoao_notifications.preferences_contract import (
    PREFERENCES_PROTOCOL,
    GetPreferencesRequest,
    IsInAppMutedRequest,
    NotificationPreferenceChannelError,
    NotificationPreferencesUnavailable,
    PatchPreferenceRequest,
    decode_get_preferences_reply,
    decode_patch_preference_reply,
    preferences_json_schema,
)
from yoao_notifications.preferences_policy import (
    DEFAULT_TOAST_TYPES,
    EXTERNAL_CHANNEL_DEFAULT_ENABLED,
    EXTERNAL_CHANNEL_ID_PATTERN,
    EXTERNAL_DELIVERABLE_TYPES,
    KNOWN_NOTIFICATION_TYPES,
    RESERVED_CHANNEL_IDS,
)
from yoao_notifications.preferences_postgres import NotificationPreferencesPostgresData

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "yoao_notifications"


def test_wire_schema_is_one_closed_document_with_all_local_references():
    package_schema = _PACKAGE / "preferences.schema.json"
    schema = json.loads(package_schema.read_text())
    assert schema == preferences_json_schema()
    assert schema["$id"] == PREFERENCES_PROTOCOL == "yoao-notification-preferences/v2"
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
    assert frozenset(policy["reserved_channel_ids"]) == RESERVED_CHANNEL_IDS
    assert (
        policy["external_channel_default_enabled"] is EXTERNAL_CHANNEL_DEFAULT_ENABLED
    )
    assert policy["external_channel_id_pattern"] == EXTERNAL_CHANNEL_ID_PATTERN
    assert frozenset(policy["external_deliverable_types"]) == EXTERNAL_DELIVERABLE_TYPES
    assert "user.deactivation_requested" not in EXTERNAL_DELIVERABLE_TYPES
    assert "user.deactivation_completed" not in EXTERNAL_DELIVERABLE_TYPES

    sql_v2 = (_PACKAGE / "sql" / "0510_notification_channels.sql").read_text()
    body = sql_v2.split(
        "CREATE OR REPLACE FUNCTION yoao_notifications_v1._preference_policy_v2()", 1
    )[1]
    body = body.split("AS $function$", 1)[1].split("$function$", 1)[0]
    rows = re.findall(
        r"\(\s*(\d+)(?:::\w+)?\s*,\s*'([^']+)'(?:::\w+)?\s*,\s*(true|false)\s*,\s*(true|false)\s*\)",
        body,
    )
    assert rows == [
        (
            str(index),
            notification_type,
            str(notification_type in DEFAULT_TOAST_TYPES).lower(),
            str(notification_type in EXTERNAL_DELIVERABLE_TYPES).lower(),
        )
        for index, notification_type in enumerate(KNOWN_NOTIFICATION_TYPES, start=1)
    ]


def test_patch_request_accepts_external_channels_and_rejects_reserved_or_empty():
    user_id = uuid.uuid4()
    request = PatchPreferenceRequest(
        schema=PREFERENCES_PROTOCOL,
        operation="patch",
        user_id=user_id,
        type="task.rejected",
        patch={"in_app": False},
    )
    assert request.external_channels is None

    ext_only = PatchPreferenceRequest(
        schema=PREFERENCES_PROTOCOL,
        operation="patch",
        user_id=user_id,
        type="task.rejected",
        external_channels={"feishu_dm": True},
    )
    assert ext_only.patch == {}
    assert ext_only.external_channels == {"feishu_dm": True}

    mute = IsInAppMutedRequest(
        schema=PREFERENCES_PROTOCOL,
        operation="is_in_app_muted",
        user_id=user_id,
        type="future.notification",
    )
    assert mute.type == "future.notification"

    with pytest.raises(ValidationError):
        PatchPreferenceRequest(
            schema=PREFERENCES_PROTOCOL,
            operation="patch",
            user_id=user_id,
            type="task.rejected",
            patch={},
        )

    for bad_channel in ("in_app", "toast", "email", "external", "UPPER", "1bad"):
        with pytest.raises(ValidationError):
            PatchPreferenceRequest(
                schema=PREFERENCES_PROTOCOL,
                operation="patch",
                user_id=user_id,
                type="task.rejected",
                external_channels={bad_channel: True},
            )


def test_reply_decoder_rejects_foreign_malformed_and_channel_errors():
    with pytest.raises(NotificationPreferencesUnavailable):
        decode_get_preferences_reply(
            {
                "schema": "aap-notification-preferences/v1",
                "ok": True,
                "data": {"operation": "get", "items": []},
            }
        )
    with pytest.raises(NotificationPreferencesUnavailable):
        decode_get_preferences_reply(
            {
                "schema": PREFERENCES_PROTOCOL,
                "ok": True,
                "data": {"operation": "get", "items": [{"type": "task.rejected"}]},
            }
        )
    with pytest.raises(NotificationPreferenceChannelError) as exc_deliverable:
        decode_patch_preference_reply(
            {
                "schema": PREFERENCES_PROTOCOL,
                "ok": False,
                "error": {"code": "channel_not_deliverable"},
            },
            "user.deactivation_requested",
        )
    assert exc_deliverable.value.code == "channel_not_deliverable"

    with pytest.raises(NotificationPreferenceChannelError) as exc_channel:
        decode_patch_preference_reply(
            {
                "schema": PREFERENCES_PROTOCOL,
                "ok": False,
                "error": {"code": "unknown_channel"},
            },
            "task.rejected",
        )
    assert exc_channel.value.code == "unknown_channel"


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
        schema=PREFERENCES_PROTOCOL,
        operation="get",
        user_id=uuid.uuid4(),
    )
    normal = _FakeSession(autoflush=True)
    await NotificationPreferencesPostgresData(normal).command(request)
    assert normal.flushes == 1
    assert "yoao_notifications_v1.preference_command" in normal.calls[0][0]
    assert json.loads(normal.calls[0][1]["request"])["operation"] == "get"

    no_autoflush = _FakeSession(autoflush=False)
    await NotificationPreferencesPostgresData(no_autoflush).command(request)
    assert no_autoflush.flushes == 0
