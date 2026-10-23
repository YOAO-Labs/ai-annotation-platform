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
