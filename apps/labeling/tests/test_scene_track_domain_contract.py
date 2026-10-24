"""Pure checks for the closed, language-neutral SceneTrack commands."""

from datetime import datetime, timezone
import json
import uuid

import pytest

from aap_labeling.scene_track_domain_contract import (
    SCENE_TRACK_DOMAIN_PROTOCOL,
    SceneTrackDomainFailure,
    SceneTrackDomainUnavailable,
    scene_track_command,
    scene_track_command_json,
    decode_scene_track_command,
    decode_scene_track_reply,
)


def test_scene_track_command_is_closed_and_decodes_owner_track_reply():
    project_id = uuid.uuid4()
    scene_id = uuid.uuid4()
    track_id = "trk-example"
    command = scene_track_command(
        "ensure",
        project_id=project_id,
        scene_id=scene_id,
        track_id=track_id,
        class_name="car",
        frames=[2, 5],
        actor_id=None,
        interval_source="manual",
        member_change=True,
    )
    payload = json.loads(scene_track_command_json(command))
    assert payload == {
        "schema": SCENE_TRACK_DOMAIN_PROTOCOL,
        "action": "ensure",
        "project_id": str(project_id),
        "scene_id": str(scene_id),
        "track_id": track_id,
        "class_name": "car",
        "frames": [2, 5],
        "actor_id": None,
        "interval_source": "manual",
        "member_change": True,
    }
    payload["is_manager"] = True
    with pytest.raises(SceneTrackDomainUnavailable):
        decode_scene_track_command(payload)

    track = {
        "id": str(uuid.uuid4()),
        "project_id": str(project_id),
        "scene_id": str(scene_id),
        "track_id": track_id,
        "class_name": "car",
        "presence_mode": "inferred",
        "attributes": None,
        "attributes_meta": ["legacy"],
        "revision": 1,
        "created_by": None,
        "retired_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    reply = {
        "schema": SCENE_TRACK_DOMAIN_PROTOCOL,
        "action": "ensure",
        "ok": True,
        "data": {
            "track": track,
            "touched": False,
            "changed": True,
            "camera_members": [],
        },
        "error": None,
    }
    result = decode_scene_track_reply(command, reply)
    assert result.track.id == uuid.UUID(track["id"])
    assert result.changed is True


def test_scene_track_failure_preserves_integrity_code():
    command = scene_track_command(
        "ensure",
        project_id=uuid.uuid4(),
        scene_id=uuid.uuid4(),
        track_id="trk-example",
        class_name="truck",
        frames=[1],
        actor_id=None,
        interval_source="manual",
        member_change=True,
    )
    with pytest.raises(SceneTrackDomainFailure) as failure:
        decode_scene_track_reply(
            command,
            {
                "schema": SCENE_TRACK_DOMAIN_PROTOCOL,
                "action": "ensure",
                "ok": False,
                "data": None,
                "error": {
                    "code": "track_class_conflict",
                    "message": "Scene Track class does not match the annotation class",
                },
            },
        )
    assert failure.value.code == "track_class_conflict"
