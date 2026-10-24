"""Pure checks for the closed SceneTrack command protocol."""

import json
from pathlib import Path
import uuid

import pytest

from aap_labeling.scene_track_command_contract import (
    SCENE_TRACK_COMMAND_PROTOCOL,
    SceneTrackCommandFailure,
    SceneTrackCommandUnavailable,
    decode_scene_track_command,
    decode_scene_track_reply,
    scene_track_command,
    scene_track_command_json,
    scene_track_command_schema,
)


def test_apply_command_round_trips_and_rejects_unlisted_fields():
    scene_id = uuid.uuid4()
    command = scene_track_command(
        "replay",
        scene_id=scene_id,
        actor_id=uuid.uuid4(),
        idempotency_key="scene-command-0001",
        request_digest="a" * 64,
    )
    payload = json.loads(scene_track_command_json(command))
    assert payload == {
        "schema": SCENE_TRACK_COMMAND_PROTOCOL,
        "action": "replay",
        "scene_id": str(scene_id),
        "actor_id": str(command.actor_id),
        "idempotency_key": "scene-command-0001",
        "request_digest": "a" * 64,
    }
    payload["actor_is_admin"] = True
    with pytest.raises(SceneTrackCommandUnavailable):
        decode_scene_track_command(payload)


def test_apply_command_requires_the_preview_scope_and_snapshot_to_match():
    scene_id = uuid.uuid4()
    track_id = "trk-command-contract"
    track_uuid = uuid.uuid4()
    task_id = uuid.uuid4()
    annotation_id = uuid.uuid4()
    interval = {
        "id": str(uuid.uuid4()),
        "start_frame": 0,
        "end_frame": None,
        "source": "manual",
        "version": 1,
    }
    payload = {
        "schema": SCENE_TRACK_COMMAND_PROTOCOL,
        "action": "apply",
        "project_id": str(uuid.uuid4()),
        "scene_id": str(scene_id),
        "actor_id": str(uuid.uuid4()),
        "idempotency_key": "terminate-scene-track",
        "request_digest": "b" * 64,
        "expected_snapshot_token": "a" * 64,
        "command": {
            "kind": "terminate",
            "track_id": track_id,
            "secondary_track_id": None,
            "frame_index": 0,
            "resume_frame": None,
            "source_annotation_id": None,
            "confirm_member_deactivation": False,
        },
        "preview": {
            "kind": "terminate",
            "scene_id": str(scene_id),
            "scene_name": None,
            "track_id": track_id,
            "secondary_track_id": None,
            "frame_index": 0,
            "resume_frame": None,
            "source_revisions": {track_id: 1},
            "before_intervals": {track_id: [interval]},
            "after_intervals": {track_id: []},
            "affected_members": {
                "total": 0,
                "by_temporal_role": {},
                "frames": [],
                "requires_confirmation": False,
            },
            "snapshot_token": "a" * 64,
        },
        "snapshot": [
            {
                "id": str(track_uuid),
                "track_id": track_id,
                "revision": 1,
                "presence_mode": "inferred",
                "retired_at": None,
                "intervals": [interval],
                "members": [
                    {
                        "id": str(annotation_id),
                        "version": 1,
                        "task_id": str(task_id),
                        "track_id": track_id,
                        "scene_track_id": str(track_uuid),
                        "class_name": "car",
                        "is_active": True,
                        "temporal_role": "keyframe",
                        "sensor_role": None,
                    }
                ],
            }
        ],
        "after_intervals": [{"track_id": track_id, "intervals": []}],
        "created_track_id": None,
        "target_task_id": None,
        "created_track_attributes": {},
        "created_track_attributes_meta": {},
    }
    command = scene_track_command(
        "apply",
        **{
            key: value
            for key, value in payload.items()
            if key not in {"schema", "action"}
        },
    )
    assert json.loads(scene_track_command_json(command)) == payload
    payload["expected_snapshot_token"] = "c" * 64
    with pytest.raises(SceneTrackCommandUnavailable):
        decode_scene_track_command(payload)


def test_command_reply_preserves_native_failure_and_schema_is_frozen():
    command = scene_track_command(
        "revert",
        scene_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        idempotency_key="scene-revert-0001",
    )
    with pytest.raises(SceneTrackCommandFailure) as failure:
        decode_scene_track_reply(
            command,
            {
                "schema": SCENE_TRACK_COMMAND_PROTOCOL,
                "action": "revert",
                "ok": False,
                "data": None,
                "error": {
                    "status_code": 409,
                    "reason": "operation_revert_stale",
                    "message": "Scene Track changed after this operation",
                },
            },
        )
    assert (failure.value.status_code, failure.value.reason) == (
        409,
        "operation_revert_stale",
    )
    schema = scene_track_command_schema()
    assert schema["$id"] == SCENE_TRACK_COMMAND_PROTOCOL
    assert (
        schema["x-postgresql-functions"]["aap_labeling_v1.scene_track_command(jsonb)"][
            "sequences"
        ]
        == []
    )
    root = Path(__file__).resolve().parents[3]
    assert (
        json.loads(
            (
                root / "apps/labeling/src/aap_labeling/scene-track-commands.schema.json"
            ).read_text(encoding="utf-8")
        )
        == schema
    )
    assert (
        json.loads(
            (
                root
                / "apps/api/alembic/sql/0343_labeling_scene_track_commands.schema.json"
            ).read_text(encoding="utf-8")
        )
        == schema
    )
    packaged_sql = (
        root
        / "apps/labeling/src/aap_labeling/sql/0343_labeling_scene_track_commands.sql"
    ).read_text(encoding="utf-8")
    migration_sql = (
        root / "apps/api/alembic/sql/0343_labeling_scene_track_commands.sql"
    ).read_text(encoding="utf-8")
    assert packaged_sql == migration_sql
