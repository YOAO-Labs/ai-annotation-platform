from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.models import MLBackendUnloadResult
from ai_annotation.project_ml import ProjectML
from ai_annotation.project_ml_models import (
    ProjectMLBackendCapabilities,
    ProjectMLBackendCreateRequest,
    ProjectMLBackendOut,
    ProjectMLBackendReloadRequest,
    ProjectMLBackendUpdateRequest,
    ProjectMLFrameInteractiveRequest,
    ProjectMLFramePredictRequest,
    ProjectMLInteractiveRequest,
    ProjectMLInteractiveResponse,
    ProjectMLPredictFrameResponse,
    ProjectMLPredictTestResponse,
    ProjectMLWarmupRequest,
)

from .conftest import API


def _id() -> str:
    return str(uuid4())


def _backend(project_id: str, backend_id: str) -> dict:
    return {
        "id": backend_id,
        "project_id": project_id,
        "name": "vision",
        "url": "https://ml.example",
        "state": "connected",
        "is_interactive": True,
        "auth_method": "token",
        "extra_params": {"timeout": 30},
        "error_message": None,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }


def _interactive_response(backend_id: str, *, frame_index: int | None = None) -> dict:
    return {
        "result": [{"type": "polygon", "points": [[0, 0], [1, 1]]}],
        "frame_index": frame_index,
        "routing": {"requested_backend_id": backend_id},
        "accept_receipts": {},
    }


def _resource(client) -> ProjectML:
    return ProjectML(client._http)


def test_project_ml_configuration_and_runtime_routes(client, respx_mock):
    project_id, backend_id = _id(), _id()
    backend = _backend(project_id, backend_id)
    create = respx_mock.post(f"{API}/projects/{project_id}/ml-backends").mock(
        return_value=httpx.Response(201, json=backend)
    )
    update = respx_mock.put(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}"
    ).mock(return_value=httpx.Response(200, json={**backend, "name": "updated"}))
    disable = respx_mock.delete(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}"
    ).mock(return_value=httpx.Response(204))
    setup = respx_mock.get(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/setup"
    ).mock(return_value=httpx.Response(200, json={"models": [{"id": "sam"}]}))
    capability_payload = {
        "name": "sam-backend",
        "protocol_version": "2",
        "models": [{"id": "sam", "supported_prompts": ["point"]}],
        "is_interactive": True,
    }
    capabilities = respx_mock.get(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/capabilities"
    ).mock(return_value=httpx.Response(200, json=capability_payload))
    refresh = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/capabilities/refresh"
    ).mock(return_value=httpx.Response(200, json=capability_payload))
    reload = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/reload"
    ).mock(return_value=httpx.Response(200, json={"reloaded": True}))
    unload = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/unload"
    ).mock(return_value=httpx.Response(200, json={"ok": True, "unloaded": True}))
    warmup = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/warmup"
    ).mock(return_value=httpx.Response(200, json={"ok": True, "loaded": "tiny"}))

    resource = _resource(client)
    created = resource.create_backend(
        project_id,
        ProjectMLBackendCreateRequest(
            name="vision",
            url="https://ml.example",
            auth_method="token",
            auth_token="create-secret",
        ),
    )
    updated = resource.update_backend(
        project_id,
        backend_id,
        ProjectMLBackendUpdateRequest(name="updated", auth_token="update-secret"),
    )
    resource.disable_backend(project_id, backend_id)
    setup_result = resource.setup(project_id, backend_id)
    capability_result = resource.capabilities(project_id, backend_id)
    refreshed = resource.refresh_capabilities(project_id, backend_id)
    reloaded = resource.reload(
        project_id,
        backend_id,
        ProjectMLBackendReloadRequest(sam_variant="tiny", modality="video"),
    )
    unloaded = resource.unload(project_id, backend_id)
    warmed = resource.warmup(
        project_id,
        backend_id,
        ProjectMLWarmupRequest.model_validate({"variant": "tiny"}),
    )

    assert isinstance(created, ProjectMLBackendOut)
    assert isinstance(updated, ProjectMLBackendOut)
    assert updated.name == "updated"
    assert setup_result["models"][0]["id"] == "sam"
    assert isinstance(capability_result, ProjectMLBackendCapabilities)
    assert capability_result.models[0].supported_prompts == ["point"]
    assert isinstance(refreshed, ProjectMLBackendCapabilities)
    assert reloaded == {"reloaded": True}
    assert isinstance(unloaded, MLBackendUnloadResult)
    assert unloaded.unloaded is True
    assert warmed == {"ok": True, "loaded": "tiny"}
    sent_create = json.loads(create.calls.last.request.content)
    sent_update = json.loads(update.calls.last.request.content)
    assert sent_create["url"] == "https://ml.example"
    assert sent_create["auth_token"] == "create-secret"
    assert sent_update == {"name": "updated", "auth_token": "update-secret"}
    assert json.loads(reload.calls.last.request.content) == {
        "sam_variant": "tiny",
        "modality": "video",
    }
    assert json.loads(warmup.calls.last.request.content) == {"variant": "tiny"}
    assert all(
        route.called
        for route in (
            create,
            update,
            disable,
            setup,
            capabilities,
            refresh,
            reload,
            unload,
            warmup,
        )
    )


def test_project_ml_inference_routes_include_multipart_frames(
    client, respx_mock, tmp_path
):
    project_id, backend_id, task_id = _id(), _id(), _id()
    interactive = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/interactive-annotating"
    ).mock(return_value=httpx.Response(200, json=_interactive_response(backend_id)))
    interactive_frame = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/interactive-annotating-frame"
    ).mock(
        return_value=httpx.Response(
            200, json=_interactive_response(backend_id, frame_index=3)
        )
    )
    predict_frame = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/predict-frame"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"prediction_id": _id(), "candidate_count": 2, "frame_index": 3},
        )
    )
    predict_test = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/predict-test"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"task_id": task_id, "result": [{"x": 1}], "score": 0.8}]
            },
        )
    )
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"jpeg-data")

    resource = _resource(client)
    transient = resource.interactive(
        project_id,
        backend_id,
        ProjectMLInteractiveRequest(
            task_id=task_id, context={"type": "point", "points": [[1, 2]]}
        ),
    )
    frame_candidates = resource.interactive_frame(
        project_id,
        backend_id,
        ProjectMLFrameInteractiveRequest(
            frame_path=frame_path,
            task_id=task_id,
            frame_index=3,
            context={"type": "point", "points": [[1, 2]]},
        ),
    )
    prediction = resource.predict_frame(
        project_id,
        backend_id,
        ProjectMLFramePredictRequest(
            frame_path=frame_path,
            task_id=task_id,
            frame_index=3,
            config={"output_mode": "box"},
        ),
    )
    prediction_test = resource.predict_test(project_id, backend_id, task_id)

    assert isinstance(transient, ProjectMLInteractiveResponse)
    assert transient.frame_index is None
    assert isinstance(frame_candidates, ProjectMLInteractiveResponse)
    assert frame_candidates.frame_index == 3
    assert isinstance(prediction, ProjectMLPredictFrameResponse)
    assert prediction.candidate_count == 2
    assert isinstance(prediction_test, ProjectMLPredictTestResponse)
    assert prediction_test.results[0].score == 0.8
    assert json.loads(interactive.calls.last.request.content)["task_id"] == task_id
    assert b'name="frame"' in interactive_frame.calls.last.request.content
    assert b"jpeg-data" in interactive_frame.calls.last.request.content
    assert b'name="context"' in interactive_frame.calls.last.request.content
    assert b'name="config"' in predict_frame.calls.last.request.content
    assert b"jpeg-data" in predict_frame.calls.last.request.content
    assert predict_test.calls.last.request.url.params["task_id"] == task_id


def test_project_ml_body_validation_and_no_local_filesystem_io(tmp_path):
    with pytest.raises(ValidationError):
        ProjectMLBackendCreateRequest(name="bad", url="http://localhost:9000")
    with pytest.raises(ValidationError):
        ProjectMLFrameInteractiveRequest(
            frame_path=tmp_path / "missing.jpg",
            task_id=_id(),
            frame_index=0,
            context={"text": "x" * (1024 * 1024)},
        )


def test_project_backend_tokens_are_hidden_from_repr_and_validation_errors():
    from ai_annotation.project_ml_models import (
        ProjectMLBackendCreateRequest,
        ProjectMLBackendUpdateRequest,
    )

    secret = "project-backend-secret"
    create = ProjectMLBackendCreateRequest(
        name="vision",
        url="https://ml.example",
        auth_method="token",
        auth_token=secret,
    )
    update = ProjectMLBackendUpdateRequest(auth_token=secret)
    assert secret not in repr(create)
    assert secret not in repr(update)
    assert create.model_dump(mode="json")["auth_token"] == secret
    assert update.model_dump(mode="json")["auth_token"] == secret

    with pytest.raises(ValidationError) as error:
        ProjectMLBackendCreateRequest(
            name="vision", url="https://ml.example", auth_token={"token": secret}
        )
    assert secret not in str(error.value)
