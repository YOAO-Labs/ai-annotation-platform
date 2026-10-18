"""Files and scenes SDK route, query, and serialization contracts."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.file_models import FileUploadInitRequest
from ai_annotation.files import Files
from ai_annotation.scene_models import Scene, SceneUpdate
from ai_annotation.scenes import Scenes

from .conftest import API

PROJECT_ID, TASK_ID, DATASET_ID, SCENE_ID, CREATOR_ID = [str(uuid4()) for _ in range(5)]

SCENE = {
    "id": SCENE_ID,
    "display_id": "SCN-1",
    "dataset_id": DATASET_ID,
    "name": "drive-01",
    "source_format": "nuscenes",
    "source_metadata": {"split": "train"},
    "created_by": CREATOR_ID,
    "created_at": "2026-10-17T00:00:00Z",
    "updated_at": "2026-10-17T00:00:00Z",
}


def test_file_upload_init_uses_typed_body_and_api_default(client, respx_mock):
    route = respx_mock.post(f"{API}/files/upload-init").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "upload_url": "https://storage.test/put?signature=abc",
                "expires_in": 900,
            },
        )
    )

    result = Files(client._http).upload_init(
        FileUploadInitRequest(project_id=PROJECT_ID, file_name="frame.jpg")
    )

    assert json.loads(route.calls.last.request.content) == {
        "project_id": PROJECT_ID,
        "file_name": "frame.jpg",
    }
    assert str(result.task_id) == TASK_ID
    assert result.expires_in == 900


def test_file_write_and_read_routes_keep_their_exact_envelopes(client, respx_mock):
    complete = respx_mock.post(f"{API}/files/upload-complete/{TASK_ID}").mock(
        return_value=httpx.Response(200, json={"status": "ok", "task_id": TASK_ID})
    )
    file_url = respx_mock.get(f"{API}/files/tasks/{TASK_ID}/file-url").mock(
        return_value=httpx.Response(
            200,
            json={"url": "https://storage.test/get?signature=xyz", "expires_in": 3600},
        )
    )
    backfill = respx_mock.post(
        f"{API}/files/projects/{PROJECT_ID}/backfill-thumbnails"
    ).mock(
        return_value=httpx.Response(
            200, json={"status": "queued", "celery_task_id": "celery-1"}
        )
    )
    files = Files(client._http)

    assert files.upload_complete(TASK_ID).status == "ok"
    assert files.file_url(TASK_ID).expires_in == 3600
    assert files.backfill_thumbnails(PROJECT_ID).celery_task_id == "celery-1"
    assert complete.called and file_url.called and backfill.called


def test_file_init_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        FileUploadInitRequest(
            project_id=PROJECT_ID,
            file_name="frame.jpg",
            unexpected="not accepted",
        )


def test_scene_list_uses_dataset_id_query_and_raw_array(client, respx_mock):
    route = respx_mock.get(f"{API}/scenes").mock(
        return_value=httpx.Response(200, json=[SCENE])
    )

    scenes = Scenes(client._http).list(DATASET_ID)

    assert route.calls.last.request.url.params["dataset_id"] == DATASET_ID
    assert isinstance(scenes[0], Scene)
    assert scenes[0].name == "drive-01"


def test_scene_get_and_trajectory_preserve_empty_pose_envelope(client, respx_mock):
    get_route = respx_mock.get(f"{API}/scenes/{SCENE_ID}").mock(
        return_value=httpx.Response(200, json=SCENE)
    )
    trajectory_route = respx_mock.get(f"{API}/scenes/{SCENE_ID}/trajectory").mock(
        return_value=httpx.Response(200, json={"scene_id": SCENE_ID, "poses": []})
    )
    scenes = Scenes(client._http)

    assert scenes.get(SCENE_ID).display_id == "SCN-1"
    trajectory = scenes.trajectory(SCENE_ID)
    assert str(trajectory.scene_id) == SCENE_ID
    assert trajectory.poses == []
    assert get_route.called and trajectory_route.called


def test_scene_update_omits_unset_fields_and_preserves_explicit_null(
    client, respx_mock
):
    idempotency_key = str(uuid4())
    route = respx_mock.patch(f"{API}/scenes/{SCENE_ID}").mock(
        return_value=httpx.Response(200, json={**SCENE, "name": "renamed"})
    )

    result = Scenes(client._http).update(
        SCENE_ID,
        SceneUpdate(name="renamed", source_format=None),
        idempotency_key=idempotency_key,
    )

    assert json.loads(route.calls.last.request.content) == {
        "name": "renamed",
        "source_format": None,
    }
    assert route.calls.last.request.headers["Idempotency-Key"] == idempotency_key
    assert result.name == "renamed"


def test_scene_request_rejects_unknown_fields_and_response_keeps_new_fields():
    with pytest.raises(ValidationError):
        SceneUpdate(unknown="value")
    scene = Scene.model_validate({**SCENE, "future_field": {"ready": True}})
    assert scene.future_field == {"ready": True}
