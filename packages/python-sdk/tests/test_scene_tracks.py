"""Scene timeline and track-command REST contracts (HTTP mocks only)."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import ConflictError
from ai_annotation.scene_track_models import (
    LegacyTrackOperationExecuteRequest,
    LegacyTrackOperationPreview,
    LegacyTrackOperationRequest,
    LegacyTrackOperationResult,
    SceneTimeline,
    SceneTrackCommandExecuteRequest,
    SceneTrackCommandPreview,
    SceneTrackCommandRequest,
    SceneTrackCommandResult,
    SceneTrackDetail,
    SceneTrackDiagnosticReport,
    SceneTrackOperationList,
    SceneTrackRevertRequest,
    TrackOperationCandidates,
)
from ai_annotation.scene_tracks import SceneTracks

from .conftest import API

TASK_ID, OTHER_TASK_ID, TRACK_ID, SECONDARY_TRACK_ID, OPERATION_ID, ANNOTATION_ID = [
    str(uuid4()) for _ in range(6)
]
SNAPSHOT_TOKEN = "a" * 64
IDEMPOTENCY_KEY = "scene-command-0001"


def _timeline() -> dict:
    return {
        "summary_version": 1,
        "scene_id": str(uuid4()),
        "scene_name": "Scene A",
        "current_frame_index": 4,
        "scene_start_frame": 0,
        "scene_end_frame": 10,
        "populated_frame_count": 8,
        "window_start_frame": 3,
        "window_end_frame": 5,
        "frames": [
            {
                "frame_index": 3,
                "state": "available",
                "task_id": TASK_ID,
                "task_status": "in_progress",
                "annotation_count": 2,
                "selected_track": {
                    "annotation_id": ANNOTATION_ID,
                    "source": "manual",
                    "class_name": "car",
                    "temporal_role": "keyframe",
                },
                "selected_track_present": True,
            },
            {
                "frame_index": 4,
                "state": "unavailable",
                "task_id": None,
                "task_status": None,
                "annotation_count": 0,
                "selected_track": None,
                "selected_track_present": None,
            },
            {"frame_index": 5, "state": "missing", "selected_track_present": False},
        ],
    }


def _track_detail() -> dict:
    return {
        "contract_version": 1,
        "id": str(uuid4()),
        "project_id": str(uuid4()),
        "scene_id": str(uuid4()),
        "scene_name": "Scene A",
        "track_id": TRACK_ID,
        "class_name": "car",
        "presence_mode": "inferred",
        "attributes": {"color": "red"},
        "attributes_meta": {},
        "revision": 7,
        "retired_at": None,
        "current_frame": 4,
        "intervals": [
            {
                "id": str(uuid4()),
                "start_frame": 0,
                "end_frame": 10,
                "source": "manual",
                "version": 3,
            }
        ],
        "members": {
            "total": 3,
            "by_temporal_role": {"keyframe": 1, "derived": 2},
            "by_source": {"manual": 1, "derived": 2},
            "keyframe_frames": [0],
            "derived_frames": [1, 2],
            "sample_frames": [],
        },
        "available_commands": ["merge", "split", "terminate"],
    }


def _summary(track_id: str) -> dict:
    return {
        "track_id": track_id,
        "class_name": "car",
        "member_count": 3,
        "first_frame": 0,
        "last_frame": 4,
    }


def _command_result(**overrides) -> dict:
    return {
        "contract_version": 1,
        "kind": "split",
        "scene_id": str(uuid4()),
        "scene_name": "Scene A",
        "track_id": TRACK_ID,
        "secondary_track_id": None,
        "frame_index": 3,
        "resume_frame": None,
        "source_revisions": {TRACK_ID: 7},
        "before_intervals": {},
        "after_intervals": {},
        "affected_members": {
            "total": 2,
            "by_temporal_role": {"keyframe": 1, "sample": 1},
            "frames": [3, 4],
            "requires_confirmation": True,
        },
        "snapshot_token": SNAPSHOT_TOKEN,
        "operation_id": OPERATION_ID,
        "status": "committed",
        "created_track_id": SECONDARY_TRACK_ID,
        "result_revisions": {TRACK_ID: 8, SECONDARY_TRACK_ID: 1},
    } | overrides


def _command_preview() -> dict:
    result = _command_result()
    return {
        key: value
        for key, value in result.items()
        if key not in {"operation_id", "status", "created_track_id", "result_revisions"}
    }


def _legacy_preview() -> dict:
    return {
        "contract_version": 1,
        "operation": "merge",
        "scene_id": str(uuid4()),
        "scene_name": "Scene A",
        "primary": _summary(TRACK_ID),
        "secondary": _summary(SECONDARY_TRACK_ID),
        "survivor_track_id": TRACK_ID,
        "affected_member_count": 6,
        "rewritten_member_count": 3,
        "snapshot_token": SNAPSHOT_TOKEN,
    }


def test_scene_timeline_and_detail_preserve_visibility_states_and_exact_paths(
    client, respx_mock
):
    timeline_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/scene-timeline").mock(
        return_value=httpx.Response(200, json=_timeline())
    )
    detail_route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/scene-tracks/{TRACK_ID}"
    ).mock(return_value=httpx.Response(200, json=_track_detail()))
    tracks = SceneTracks(client._http)

    timeline = tracks.scene_timeline(
        TASK_ID, start_frame=3, end_frame=5, track_id=TRACK_ID
    )
    detail = tracks.get_track(TASK_ID, TRACK_ID)

    assert isinstance(timeline, SceneTimeline)
    assert [frame.state for frame in timeline.frames] == [
        "available",
        "unavailable",
        "missing",
    ]
    assert timeline.frames[1].selected_track_present is None
    assert isinstance(detail, SceneTrackDetail)
    assert detail.revision == 7 and detail.members.total == 3
    assert dict(timeline_route.calls.last.request.url.params) == {
        "start_frame": "3",
        "end_frame": "5",
        "track_id": TRACK_ID,
    }
    assert detail_route.called

    with pytest.raises(ValueError, match="200 frames"):
        tracks.scene_timeline(TASK_ID, start_frame=0, end_frame=200)
    assert timeline_route.call_count == 1


def test_track_id_is_encoded_without_changing_the_openapi_placeholder(
    client, respx_mock
):
    track_id = "car/segment 2"
    route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/scene-tracks/car%2Fsegment%202"
    ).mock(
        return_value=httpx.Response(200, json={**_track_detail(), "track_id": track_id})
    )

    result = SceneTracks(client._http).get_track(TASK_ID, track_id)

    assert result.track_id == track_id
    assert route.called


def test_diagnostics_history_and_candidate_limits_match_api(client, respx_mock):
    diagnostics_route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/scene-track-diagnostics"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "contract_version": 1,
                "scene_id": str(uuid4()),
                "track_count": 2,
                "linked_member_count": 5,
                "issue_counts": {"missing_member": 1},
                "issues": [
                    {
                        "code": "missing_member",
                        "track_id": TRACK_ID,
                        "annotation_id": ANNOTATION_ID,
                        "frame_index": 3,
                    }
                ],
                "truncated": False,
            },
        )
    )
    history_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/scene-track-commands").mock(
        return_value=httpx.Response(
            200,
            json={"contract_version": 1, "operations": []},
        )
    )
    candidates_route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/track-operations/candidates"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "contract_version": 1,
                "primary": _summary(TRACK_ID),
                "candidates": [_summary(SECONDARY_TRACK_ID)],
                "truncated": False,
            },
        )
    )
    tracks = SceneTracks(client._http)

    diagnostics = tracks.diagnostics(TASK_ID, limit=300)
    history = tracks.list_command_operations(TASK_ID, TRACK_ID, limit=40)
    candidates = tracks.list_track_operation_candidates(TASK_ID, TRACK_ID, limit=20)

    assert isinstance(diagnostics, SceneTrackDiagnosticReport)
    assert diagnostics.issues[0].code == "missing_member"
    assert isinstance(history, SceneTrackOperationList)
    assert isinstance(candidates, TrackOperationCandidates)
    assert dict(diagnostics_route.calls.last.request.url.params) == {"limit": "300"}
    assert dict(history_route.calls.last.request.url.params) == {
        "track_id": TRACK_ID,
        "limit": "40",
    }
    assert dict(candidates_route.calls.last.request.url.params) == {
        "track_id": TRACK_ID,
        "limit": "20",
    }
    with pytest.raises(ValueError, match="between 1 and 20"):
        tracks.list_track_operation_candidates(TASK_ID, TRACK_ID, limit=21)
    assert candidates_route.call_count == 1


def test_preferred_command_preview_execute_and_original_operation_revert(
    client, respx_mock
):
    preview_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/scene-track-commands/preview"
    ).mock(return_value=httpx.Response(200, json=_command_preview()))
    execute_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/scene-track-commands/execute"
    ).mock(return_value=httpx.Response(200, json=_command_result()))
    revert_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/scene-track-commands/{OPERATION_ID}/revert"
    ).mock(
        return_value=httpx.Response(
            200,
            json=_command_result(
                kind="revert", status="committed", created_track_id=None
            ),
        )
    )
    tracks = SceneTracks(client._http)
    command = SceneTrackCommandRequest(
        kind="split",
        track_id=TRACK_ID,
        frame_index=3,
        confirm_member_deactivation=True,
    )

    preview = tracks.preview_command(TASK_ID, command)
    execute_body = SceneTrackCommandExecuteRequest(
        kind="split",
        track_id=TRACK_ID,
        frame_index=3,
        confirm_member_deactivation=True,
        snapshot_token=preview.snapshot_token,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    executed = tracks.execute_command(TASK_ID, execute_body)
    reverted = tracks.revert_command(
        TASK_ID,
        OPERATION_ID,
        SceneTrackRevertRequest(idempotency_key="scene-revert-0001"),
    )

    assert isinstance(preview, SceneTrackCommandPreview)
    assert preview.affected_members.requires_confirmation is True
    assert isinstance(executed, SceneTrackCommandResult)
    assert dict(json.loads(execute_route.calls.last.request.content)) == {
        "kind": "split",
        "track_id": TRACK_ID,
        "frame_index": 3,
        "confirm_member_deactivation": True,
        "snapshot_token": SNAPSHOT_TOKEN,
        "idempotency_key": IDEMPOTENCY_KEY,
    }
    assert json.loads(preview_route.calls.last.request.content) == {
        "kind": "split",
        "track_id": TRACK_ID,
        "frame_index": 3,
        "confirm_member_deactivation": True,
    }
    assert json.loads(revert_route.calls.last.request.content) == {
        "idempotency_key": "scene-revert-0001"
    }
    assert isinstance(reverted, SceneTrackCommandResult)
    assert reverted.kind == "revert"


def test_stale_snapshot_is_returned_without_refresh_or_retry(client, respx_mock):
    route = respx_mock.post(f"{API}/tasks/{TASK_ID}/scene-track-commands/execute").mock(
        return_value=httpx.Response(
            409, json={"detail": {"reason": "track_snapshot_stale"}}
        )
    )

    with pytest.raises(ConflictError):
        SceneTracks(client._http).execute_command(
            TASK_ID,
            SceneTrackCommandExecuteRequest(
                kind="mark_absent",
                track_id=TRACK_ID,
                frame_index=3,
                snapshot_token=SNAPSHOT_TOKEN,
                idempotency_key=IDEMPOTENCY_KEY,
            ),
        )

    assert route.call_count == 1


def test_legacy_split_merge_preview_and_execute_remain_separate(client, respx_mock):
    preview_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/track-operations/preview"
    ).mock(return_value=httpx.Response(200, json=_legacy_preview()))
    execute_response = {
        **_legacy_preview(),
        "created_track_id": None,
        "updated_member_count": 3,
    }
    execute_route = respx_mock.post(f"{API}/tasks/{TASK_ID}/track-operations").mock(
        return_value=httpx.Response(200, json=execute_response)
    )
    tracks = SceneTracks(client._http)
    request = LegacyTrackOperationRequest(
        operation="merge",
        primary_track_id=TRACK_ID,
        secondary_track_id=SECONDARY_TRACK_ID,
    )

    preview = tracks.preview_track_operation(TASK_ID, request)
    executed = tracks.execute_track_operation(
        TASK_ID,
        LegacyTrackOperationExecuteRequest(
            operation="merge",
            primary_track_id=TRACK_ID,
            secondary_track_id=SECONDARY_TRACK_ID,
            snapshot_token=preview.snapshot_token,
        ),
    )

    assert isinstance(preview, LegacyTrackOperationPreview)
    assert isinstance(executed, LegacyTrackOperationResult)
    assert json.loads(preview_route.calls.last.request.content) == {
        "operation": "merge",
        "primary_track_id": TRACK_ID,
        "secondary_track_id": SECONDARY_TRACK_ID,
    }
    execute_body = json.loads(execute_route.calls.last.request.content)
    assert execute_body["snapshot_token"] == SNAPSHOT_TOKEN
    assert "idempotency_key" not in execute_body


def test_command_request_models_reject_ambiguous_fields():
    with pytest.raises(ValidationError, match="two different tracks"):
        SceneTrackCommandRequest(
            kind="merge", track_id=TRACK_ID, secondary_track_id=TRACK_ID
        )
    with pytest.raises(ValidationError, match="resume_frame is required"):
        SceneTrackCommandRequest(kind="resume", track_id=TRACK_ID)
    with pytest.raises(ValidationError, match="required for split"):
        LegacyTrackOperationRequest(operation="split", primary_track_id=TRACK_ID)
