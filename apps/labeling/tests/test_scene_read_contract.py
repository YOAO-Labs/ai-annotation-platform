from __future__ import annotations

import json
import uuid

import pytest

from aap_labeling.scene_read_contract import (
    SceneReadUnavailable,
    decode_scene_read_reply,
    scene_read_request,
    scene_read_schema,
)


def _reply(request, *, primary_item_id: uuid.UUID | None = None) -> dict:
    return {
        "schema": "aap-labeling-scene-read/v1",
        "action": request.action,
        "request": request.model_dump(mode="json", by_alias=True),
        "data": {
            "primary_item_id": primary_item_id,
            "frames": [],
            "frame_tasks": [],
            "timeline_tasks": [],
            "annotation_summaries": [],
            "track": None,
            "tracks": [],
            "track_cache": None,
            "camera_members": [],
            "intervals": [],
            "members": [],
            "candidate_counts": [],
        },
    }


def test_scene_read_contract_rejects_unknown_actions_and_reply_mismatch():
    task_id = uuid.uuid4()
    request = scene_read_request("resolve_primary_link", task_id=task_id)
    reply = decode_scene_read_reply(
        request,
        json.dumps(_reply(request, primary_item_id=uuid.uuid4()), default=str),
    )
    assert reply.primary_item_id is not None

    with pytest.raises(SceneReadUnavailable):
        scene_read_request("arbitrary_sql", task_id=task_id)
    with pytest.raises(SceneReadUnavailable):
        decode_scene_read_reply(
            request,
            _reply(scene_read_request("resolve_primary_link", task_id=uuid.uuid4())),
        )


def test_scene_read_schema_has_one_function_only_owner_capability():
    function = scene_read_schema()["x-postgresql-functions"]
    assert list(function) == ["aap_labeling_v1.scene_read_query(jsonb)"]
    assert function["aap_labeling_v1.scene_read_query(jsonb)"]["actions"] == [
        "resolve_primary_link",
        "resolve_task_frames",
        "scene_frame_tasks",
        "timeline_tasks",
        "timeline_annotation_summaries",
        "load_command_track",
        "load_track_detail",
        "load_timeline_track",
        "load_scene_diagnostics",
        "load_scene_track_cache",
        "load_scene_camera_cache",
        "read_track_members",
        "lock_track_members",
        "candidate_track_counts",
    ]
