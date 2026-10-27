"""Task ownership, legacy request identity and isolated current-model views."""

import uuid

import pytest
from fastapi import HTTPException
from training_contract.model_return import segmentation_identity
from app.config import settings

from app.db.models.trained_model import TrainedModelVersion
from app.schemas.trained_model import ModelReleaseCreate
from app.services import trained_models as service, model_lineage
from tests.test_trained_models import (
    controlled_artifacts,  # noqa: F401
    headers,
    mock_live_receipts,
    next_successful_attempt,
    publish,
    ready_deployment,
    register,
    seed_source,
)


def test_detect_release_request_keeps_legacy_canonical_bytes():
    request = {"request_id": str(uuid.uuid4()), "expected_generation": 0}
    implicit = ModelReleaseCreate.model_validate(request)
    explicit = ModelReleaseCreate.model_validate({**request, "task": "detect"})
    legacy = implicit.model_dump(mode="json", exclude={"task"})
    assert implicit.request_payload() == explicit.request_payload() == legacy
    assert (
        ModelReleaseCreate.model_validate(
            {**request, "task": "segment"}
        ).request_payload()["task"]
        == "segment"
    )


@pytest.mark.usefixtures("controlled_artifacts")
async def test_task_scoped_generations_history_cas_and_current_metadata(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    a, _ = await register(httpx_client, token, project, run, attempt)
    segment_attempt = await next_successful_attempt(db_session, run, attempt)
    b, _ = await register(
        httpx_client, token, project, run, segment_attempt, name="Segmentation fixture"
    )
    detect = await db_session.get(TrainedModelVersion, uuid.UUID(a["id"]))
    segment = await db_session.get(TrainedModelVersion, uuid.UUID(b["id"]))
    segment.provenance = {
        **segment.provenance,
        "task": "segment",
        "base_model": {"id": "yolo11n-seg"},
        "model_return": segmentation_identity("yolo11n-seg"),
    }
    project.tool_bindings = {
        **project.tool_bindings,
        "region": project.tool_bindings["bbox"],
    }
    project.raster_mask_native_editing_enabled = True
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.flush()
    _, d1 = await ready_deployment(db_session, project, detect)
    _, d2 = await ready_deployment(db_session, project, segment)
    mock_live_receipts(monkeypatch, db_session)
    first, _ = await publish(httpx_client, token, project, detect, d1, 0)
    second, body = await publish(
        httpx_client, token, project, segment, d2, 0, task="segment"
    )
    assert first["generation"] == second["generation"] == 1
    assert first["task"] == "detect" and second["task"] == "segment"
    route = f"/api/v1/projects/{project.id}/model-releases"
    default = (await httpx_client.get(route, headers=headers(token))).json()
    split = (
        await httpx_client.get(
            route, params={"task": "segment"}, headers=headers(token)
        )
    ).json()
    assert [row["id"] for row in default["items"]] == [first["id"]]
    assert [row["id"] for row in split["items"]] == [second["id"]]
    assert default["current_release_id"] == first["id"]
    assert split["current_release_id"] == second["id"]
    metadata = await model_lineage.model_metadata(
        db_session, project.id, [detect.id, segment.id]
    )
    assert all(row["is_current_release"] for row in metadata.values())
    mismatch = await httpx_client.post(
        route,
        headers=headers(token),
        json={
            **body,
            "request_id": str(uuid.uuid4()),
            "task": "detect",
            "expected_generation": 1,
        },
    )
    assert mismatch.status_code == 422
    reused = await httpx_client.post(
        route, headers=headers(token), json={**body, "task": "detect"}
    )
    assert reused.status_code == 409
    cross_rollback = await httpx_client.post(
        route,
        headers=headers(token),
        json={
            "request_id": str(uuid.uuid4()),
            "task": "segment",
            "expected_generation": 1,
            "rollback_from_id": first["id"],
        },
    )
    assert cross_rollback.status_code == 422
    stale = await httpx_client.post(
        route,
        headers=headers(token),
        json={
            "request_id": str(uuid.uuid4()),
            "task": "segment",
            "expected_generation": 0,
        },
    )
    assert stale.status_code == 409
    by_task = await httpx_client.get(
        f"/api/v1/projects/{project.id}/trained-models",
        params={"task": "segment"},
        headers=headers(token),
    )
    assert [row["id"] for row in by_task.json()["items"]] == [str(segment.id)]


@pytest.mark.usefixtures("controlled_artifacts")
async def test_segment_release_alias_never_uses_detect_selection(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    mock_live_receipts(monkeypatch, db_session)
    await publish(httpx_client, token, project, version, deployment, 0)
    with pytest.raises(HTTPException) as error:
        await service.resolve_reference(
            db_session, project, "project-release:segment", backend.id
        )
    assert getattr(error.value, "status_code", None) == 409
    assert service.is_controlled("project-release:segment")
    assert not service.is_controlled("project-release:unknown")
