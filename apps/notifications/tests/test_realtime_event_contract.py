from __future__ import annotations

import json
import math
from importlib.resources import files
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from aap_notifications.realtime_event_contract import (
    NOTIFICATION_REALTIME_PROTOCOL,
    NotificationSyncEvent,
    notification_event_message,
    notification_realtime_json_schema,
    notification_sync_message,
)


SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "aap_notifications"
    / "realtime_event.schema.json"
)
NODE_PROVIDER = (
    Path(__file__).parents[1] / "conformance" / "node" / "realtime_event_provider.mjs"
)


def test_frozen_realtime_schema_matches_contract_models():
    frozen = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    packaged = files("aap_notifications").joinpath("realtime_event.schema.json")

    assert frozen == notification_realtime_json_schema()
    assert json.loads(packaged.read_text(encoding="utf-8")) == frozen
    assert frozen["$id"] == NOTIFICATION_REALTIME_PROTOCOL
    assert frozen["$defs"]["NotificationEvent"]["additionalProperties"] is False
    assert frozen["$defs"]["NotificationSyncEvent"]["additionalProperties"] is True
    payload_schema = frozen["$defs"]["NotificationEvent"]["properties"]["payload"]
    assert "type" not in payload_schema
    assert "anyOf" not in payload_schema
    assert frozen["$defs"]["NotificationEvent"]["properties"]["id"]["maxLength"] == 36


def test_event_builder_preserves_existing_field_values_and_order():
    message = notification_event_message(
        id="60000000-0000-4000-8000-000000000001",
        type="future.module_event",
        target_type="future_target",
        target_id="60000000-0000-4000-8000-000000000002",
        payload={"text": "中文", "value": math.nan},
        created_at="2026-10-24T10:00:00+05:30",
    )

    assert list(message) == [
        "id",
        "type",
        "target_type",
        "target_id",
        "payload",
        "created_at",
    ]
    assert message["type"] == "future.module_event"
    assert message["target_type"] == "future_target"
    assert message["created_at"] == "2026-10-24T10:00:00+05:30"
    assert math.isnan(message["payload"]["value"])
    assert "\\u4e2d\\u6587" in json.dumps(message)


def test_event_builder_does_not_coerce_wire_strings():
    with pytest.raises(ValidationError):
        notification_event_message(
            id=60000000,
            type="future.module_event",
            target_type="future_target",
            target_id="60000000-0000-4000-8000-000000000002",
            payload={},
            created_at="2026-10-24T10:00:00+05:30",
        )


def test_event_builder_preserves_valid_uuid_and_datetime_strings():
    message = notification_event_message(
        id="A0000000-0000-4000-8000-000000000001",
        type="future.module_event",
        target_type="task",
        target_id="B0000000-0000-4000-8000-000000000002",
        payload=None,
        created_at="2026-10-24T10:00:00Z",
    )

    assert message["id"] == "A0000000-0000-4000-8000-000000000001"
    assert message["target_id"] == "B0000000-0000-4000-8000-000000000002"
    assert message["created_at"] == "2026-10-24T10:00:00Z"


@pytest.mark.parametrize(
    "payload",
    [None, [], ["legacy"], "legacy", 7, True, 1.5, {"legacy": "object"}],
)
def test_event_builder_preserves_native_jsonb_payload_shapes(payload):
    message = notification_event_message(
        id="60000000-0000-4000-8000-000000000001",
        type="legacy.notification",
        target_type="task",
        target_id="60000000-0000-4000-8000-000000000002",
        payload=payload,
        created_at="2026-10-24T10:00:00+05:30",
    )

    assert message["payload"] == payload


def test_event_builder_preserves_deep_native_jsonb_payload():
    payload = {}
    nested = payload
    for _ in range(350):
        nested["next"] = {}
        nested = nested["next"]

    message = notification_event_message(
        id="60000000-0000-4000-8000-000000000001",
        type="legacy.notification",
        target_type="task",
        target_id="60000000-0000-4000-8000-000000000002",
        payload=payload,
        created_at="2026-10-24T10:00:00+05:30",
    )

    assert json.dumps(message) == json.dumps(
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "legacy.notification",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": payload,
            "created_at": "2026-10-24T10:00:00+05:30",
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("id", "60000000-0000-4000-8000-000000000001\n"),
        ("target_id", "also-invalid"),
        ("created_at", "not-a-time"),
        ("created_at", "2026-10-24T10:00:00+05:30\n"),
        ("created_at", "2026-02-31T10:00:00+05:30"),
        ("created_at", "2026-10-24T24:00:00+05:30"),
        ("created_at", "2026-10-24T10:00:00"),
        ("created_at", "2026-10-24T10:00:00+24:00"),
    ],
)
def test_event_builder_rejects_invalid_uuid_and_datetime_without_rewriting(
    field, value
):
    message = {
        "id": "60000000-0000-4000-8000-000000000001",
        "type": "legacy.notification",
        "target_type": "task",
        "target_id": "60000000-0000-4000-8000-000000000002",
        "payload": None,
        "created_at": "2026-10-24T10:00:00+05:30",
    }
    message[field] = value

    with pytest.raises(ValidationError):
        notification_event_message(**message)


def test_event_string_limits_count_unicode_codepoints():
    message = notification_event_message(
        id="60000000-0000-4000-8000-000000000001",
        type="😀" * 60,
        target_type="🎯" * 30,
        target_id="60000000-0000-4000-8000-000000000002",
        payload={},
        created_at="2026-10-24T10:00:00+05:30",
    )
    assert len(message["type"]) == 60
    assert len(message["target_type"]) == 30

    with pytest.raises(ValidationError):
        notification_event_message(
            id="60000000-0000-4000-8000-000000000001",
            type="future.module_event",
            target_type="🎯" * 31,
            target_id="60000000-0000-4000-8000-000000000002",
            payload={},
            created_at="2026-10-24T10:00:00+05:30",
        )


def test_sync_builder_and_model_keep_open_reason_and_extensions():
    assert notification_sync_message(reason="preferences") == {
        "type": "notifications.sync",
        "reason": "preferences",
    }
    assert NotificationSyncEvent.model_validate(
        {"type": "notifications.sync", "marker": "before"}
    ).model_dump(mode="python", exclude_unset=True) == {
        "type": "notifications.sync",
        "marker": "before",
    }
    assert NotificationSyncEvent.model_validate(
        {"type": "notifications.sync"}
    ).model_dump(mode="python", exclude_unset=True) == {
        "type": "notifications.sync",
    }


def test_node_stdio_peer_accepts_and_preserves_existing_message_shapes():
    samples = [
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "future.module_event",
            "target_type": "future_target",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": {"text": "中文"},
            "created_at": "2026-10-24T10:00:00+05:30",
        },
        {
            "id": "A0000000-0000-4000-8000-000000000001",
            "type": "future.scalar_payload",
            "target_type": "task",
            "target_id": "B0000000-0000-4000-8000-000000000002",
            "payload": None,
            "created_at": "2026-10-24T10:00:00Z",
        },
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "future.array_payload",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": ["legacy"],
            "created_at": "2026-10-24T10:00:00+00:00",
        },
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "future.scalar_payload",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": "legacy",
            "created_at": "2026-10-24T10:00:00+00:00",
        },
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "future.scalar_payload",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": 7,
            "created_at": "2026-10-24T10:00:00+00:00",
        },
        {
            "id": "60000000-0000-4000-8000-000000000001",
            "type": "future.scalar_payload",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000002",
            "payload": True,
            "created_at": "2026-10-24T10:00:00+00:00",
        },
        {"type": "notifications.sync", "marker": "before"},
        {
            "type": "notifications.sync",
            "reason": "future.reason",
            "marker": "before",
        },
    ]

    for sample in samples:
        encoded = json.dumps(sample, ensure_ascii=True)
        result = subprocess.run(
            ["node", str(NODE_PROVIDER), "--stdio"],
            input=encoded,
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == encoded


def test_node_stdio_peer_rejects_malformed_event_without_echoing_it():
    sample = {
        "type": "future.module_event",
        "target_type": "future_target",
        "target_id": "60000000-0000-4000-8000-000000000002",
        "payload": {"secret": "redis://user:secret@cache"},
        "created_at": "2026-10-24T10:00:00+05:30",
    }
    result = subprocess.run(
        ["node", str(NODE_PROVIDER), "--stdio"],
        input=json.dumps(sample),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "invalid notification realtime message\n"
    assert "redis://user:secret@cache" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("id", "60000000-0000-4000-8000-000000000001\n"),
        ("target_id", "also-invalid"),
        ("created_at", "not-a-time"),
        ("created_at", "2026-10-24T10:00:00+05:30\n"),
        ("created_at", "2026-02-31T10:00:00+05:30"),
        ("created_at", "2026-10-24T24:00:00+05:30"),
        ("created_at", "2026-10-24T10:00:00"),
        ("created_at", "2026-10-24T10:00:00+24:00"),
    ],
)
def test_node_stdio_rejects_the_same_invalid_uuid_and_datetime(field, value):
    sample = {
        "id": "60000000-0000-4000-8000-000000000001",
        "type": "legacy.notification",
        "target_type": "task",
        "target_id": "60000000-0000-4000-8000-000000000002",
        "payload": None,
        "created_at": "2026-10-24T10:00:00+05:30",
    }
    sample[field] = value
    result = subprocess.run(
        ["node", str(NODE_PROVIDER), "--stdio"],
        input=json.dumps(sample),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "invalid notification realtime message\n"
