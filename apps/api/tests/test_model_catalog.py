"""Discovery preserves project boundaries and cannot trigger model loading."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from aap_inference.backend_transport import BackendTransport
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.services import inference_backend_control
from tests.conftest import create_registry_with_pool
from tests.factory import create_membership, create_project
from tests.test_trained_models import (
    controlled_artifacts as controlled_artifacts,
    mock_live_receipts,
    publish,
    ready_deployment,
    register,
    seed_source,
)


def headers(token):
    return {"Authorization": f"Bearer {token}"}


async def builtin(db, project):
    backend, pool = await create_registry_with_pool(
        db,
        name="catalog-yolo",
        enabled_pool=True,
        is_interactive=False,
        last_checked_at=datetime.now(timezone.utc),
        health_meta={
            "capabilities": {
                "models": [
                    {
                        "id": "detect",
                        "display_name": "YOLO",
                        "task": "detect",
                        "supported_prompts": ["none"],
                        "supported_inputs": ["full_image", "crop"],
                        "supported_geometric_outputs": ["bbox"],
                        "resource_profile": {"device": "gpu", "batchable": True},
                        "supported_variants": [
                            {
                                "key": "series",
                                "variants": [{"value": "v1"}, {"value": "v2"}],
                            },
                            {
                                "key": "size",
                                "variants": [{"value": "n"}, {"value": "s"}],
                            },
                        ],
                        "variant_combinations": [["v1", "n"], ["v2", "s"]],
                        "classes": [{"index": 0, "name": "car"}],
                    }
                ]
            }
        },
        auth_method="token",
        auth_token="private-token-" + "x" * 40,
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    await db.flush()
    return backend, pool


async def test_builtin_directory_uses_valid_combinations_without_probing(
    db_session,
    httpx_client,
    project_admin,
    monkeypatch,
):
    owner, token = project_admin
    project = await create_project(db_session, owner_id=owner.id)
    backend, _ = await builtin(db_session, project)
    probe = AsyncMock(side_effect=AssertionError("directory must not probe"))
    monkeypatch.setattr(BackendTransport, "setup", probe)

    def no_control(*_args, **_kwargs):
        raise AssertionError("directory must not probe the control owner")

    monkeypatch.setattr(
        inference_backend_control, "backend_control_service", no_control
    )
    response = await httpx_client.get(
        f"/api/v1/model-catalog?project_id={project.id}", headers=headers(token)
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    assert {tuple(sorted(e["model_variants"].items())) for e in payload["items"]} == {
        (("series", "v1"), ("size", "n")),
        (("series", "v2"), ("size", "s")),
    }
    assert all(e["operations"]["preannotate"]["allowed"] for e in payload["items"])
    assert all(
        e["class_metadata"]["verification_status"] == "declared"
        for e in payload["items"]
    )
    assert backend.url not in response.text and backend.auth_token not in response.text
    probe.assert_not_called()
    backend.state = "error"
    await db_session.flush()
    offline = (
        await httpx_client.get(
            f"/api/v1/model-catalog?project_id={project.id}&page_size=1",
            headers=headers(token),
        )
    ).json()
    assert offline["total"] == 2 and len(offline["items"]) == 1
    assert (
        offline["items"][0]["operations"]["preannotate"]["reason_code"]
        == "backend_unavailable"
    )


@pytest.mark.usefixtures("controlled_artifacts")
async def test_private_candidates_and_published_models_have_distinct_permissions(
    db_session,
    httpx_client,
    project_admin,
    annotator,
    monkeypatch,
):
    owner, token = project_admin
    member, member_token = annotator
    project, run, attempt = await seed_source(db_session, owner)
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="annotator"
    )
    created, _ = await register(httpx_client, token, project, run, attempt)
    from app.db.models.trained_model import TrainedModelVersion
    import uuid

    version = await db_session.get(TrainedModelVersion, uuid.UUID(created["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    route = f"/api/v1/projects/{project.id}/model-catalog"
    candidate = (await httpx_client.get(route, headers=headers(token))).json()
    row = next(e for e in candidate["items"] if e["version_id"] == str(version.id))
    assert row["operations"]["trial"]["allowed"]
    assert row["operations"]["preannotate"]["reason_code"] == "not_published"
    member_catalog = await httpx_client.get(route, headers=headers(member_token))
    assert member_catalog.status_code == 200
    assert not any(
        e["source"] == "project_trained" for e in member_catalog.json()["items"]
    )
    mock_live_receipts(monkeypatch, db_session)
    released, _ = await publish(httpx_client, token, project, version, deployment, 0)
    assert released["model_version_id"] == str(version.id)
    published = (await httpx_client.get(route, headers=headers(member_token))).json()
    row = next(e for e in published["items"] if e["version_id"] == str(version.id))
    assert row["publication"] == "current"
    assert row["class_metadata"]["verification_status"] == "verified"
    assert row["operations"]["workbench"]["allowed"]
    assert not row["operations"]["preannotate"]["allowed"]
    assert not row["operations"]["trial"]["allowed"]
    assert published["current_release"]["registry_id"] == str(backend.id)
    assert version.artifact["key"] not in str(published)
    assert (
        await httpx_client.get("/api/v1/model-catalog", headers=headers(member_token))
    ).status_code == 403

    backend.state = "error"
    await db_session.flush()
    offline = (await httpx_client.get(route, headers=headers(member_token))).json()
    row = next(e for e in offline["items"] if e["version_id"] == str(version.id))
    assert row["class_metadata"]["verification_status"] == "verified"
    assert row["class_metadata"]["class_source"] == "checkpoint"
    assert not row["operations"]["workbench"]["allowed"]
    assert offline["current_release"] is None


async def test_project_membership_does_not_grant_global_management(
    db_session,
    httpx_client,
    project_admin,
    super_admin,
):
    owner, _ = super_admin
    member, token = project_admin
    project = await create_project(db_session, owner_id=owner.id)
    # Project administrators can be ordinary members elsewhere, never managers there.
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="viewer"
    )
    response = await httpx_client.get(
        f"/api/v1/model-catalog?project_id={project.id}", headers=headers(token)
    )
    assert response.status_code == 404
