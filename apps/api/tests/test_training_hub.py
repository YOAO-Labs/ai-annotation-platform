"""Training hub discovery preserves management scope and execution identity."""

import uuid
from datetime import timedelta

import pytest
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingResourceLease, TrainingRun
from app.services import training_runs as service
from app.services import training_adapters as adapters

from tests.factory import create_membership, create_project
from tests.test_training_runs import (  # noqa: F401
    GPU,
    IMAGE,
    ready_dataset,
    training_config,
)

pytestmark = pytest.mark.usefixtures("training_config")


def headers(principal):
    return {"Authorization": f"Bearer {principal[1]}"}


async def add_run(
    db, project, dataset, creator, *, status="queued", framework="ultralytics"
):
    run = TrainingRun(
        project_id=project.id,
        creator_id=creator.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=f"{framework}-{status}",
        status=status,
        config={"framework": framework, "parameters": {"epochs": 10}},
        config_digest="b" * 64,
    )
    db.add(run)
    await db.flush()
    from app.services.training_run_storage import allocate

    await allocate(db, run)
    return run


async def test_hub_scope_includes_owned_runs_by_other_creators_but_not_memberships(
    db_session, httpx_client, project_admin, super_admin
):
    owner = project_admin[0]
    owned, dataset = await ready_dataset(db_session, owner)
    foreign, foreign_dataset = await ready_dataset(db_session, super_admin[0])
    await create_membership(
        db_session, project_id=foreign.id, user_id=owner.id, role="viewer"
    )
    expected = await add_run(db_session, owned, dataset, super_admin[0])
    await add_run(
        db_session, owned, dataset, owner, status="succeeded", framework="libreyolo"
    )
    await add_run(
        db_session, foreign, foreign_dataset, super_admin[0], status="training"
    )
    response = await httpx_client.get(
        "/api/v1/training/runs", headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["run"]["id"] == str(expected.id)
    assert page["counts"]["active"] == 1
    assert page["counts"]["running"] == 0
    assert page["counts"]["succeeded"] == 1
    response = await httpx_client.get(
        "/api/v1/training/projects", headers=headers(project_admin)
    )
    assert [item["id"] for item in response.json()["items"]] == [str(owned.id)]
    response = await httpx_client.get(
        f"/api/v1/training/runs?project_id={foreign.id}&state=all",
        headers=headers(project_admin),
    )
    assert response.json()["total"] == 0
    assert sum(response.json()["counts"].values()) == 0
    response = await httpx_client.get(
        "/api/v1/training/runs?state=finished&framework=libreyolo&limit=1",
        headers=headers(project_admin),
    )
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["run"]["status"] == "succeeded"
    assert response.json()["counts"]["active"] == 0
    response = await httpx_client.get(
        "/api/v1/training/runs?state=all&limit=1&offset=1",
        headers=headers(project_admin),
    )
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1


async def test_project_readiness_uses_frozen_versions_and_reports_configuration_gaps(
    db_session, httpx_client, super_admin
):
    project, dataset = await ready_dataset(db_session, super_admin[0])
    project.name = "Ready % literal"
    await add_run(db_session, project, dataset, super_admin[0], status="lost")
    unsupported = await create_project(db_session, owner_id=super_admin[0].id)
    unsupported.data_type = "video"
    unconfigured = await create_project(db_session, owner_id=super_admin[0].id)
    unconfigured.tool_bindings = {}
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/projects", headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[str(project.id)]["ready_dataset_count"] == 1
    assert items[str(project.id)]["active_run_count"] == 1
    assert items[str(project.id)]["can_prepare_dataset"]
    assert not items[str(unsupported.id)]["can_prepare_dataset"]
    assert "图片" in items[str(unsupported.id)]["preparation_reason"]
    assert "矩形框" in items[str(unconfigured.id)]["preparation_reason"]
    response = await httpx_client.get(
        "/api/v1/training/projects?search=%25", headers=headers(super_admin)
    )
    assert response.json()["total"] == 1
    dataset.package_sha256 = None
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/projects?search=%25", headers=headers(super_admin)
    )
    assert response.json()["items"][0]["ready_dataset_count"] == 0


async def test_catalog_distinguishes_heartbeat_and_template_compatibility(
    db_session, httpx_client, super_admin
):
    lease = TrainingResourceLease(
        resource_id=adapters.resource_id(),
        executor_id="actual-container",
        last_seen_at=service.now(),
        runtime={
            "gpu_uuid": GPU,
            "image_digest": IMAGE,
            "frameworks": {"ultralytics": "8.4.177"},
            "private_token": "must-not-leak",
        },
    )
    db_session.add(lease)
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/catalog", headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["online"] and response.json()["items"][0]["compatible"]
    assert response.json()["executor_id"] == "actual-container"
    assert "must-not-leak" not in response.text
    lease.runtime = {**lease.runtime, "image_digest": "sha256:" + "f" * 64}
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/catalog", headers=headers(super_admin)
    )
    assert response.json()["online"] and not response.json()["items"][0]["compatible"]
    lease.last_seen_at = service.now() - timedelta(seconds=31)
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/catalog", headers=headers(super_admin)
    )
    assert not response.json()["online"]
    assert response.json()["items"][0]["compatible"] is None


async def test_segmentation_only_catalog_reports_qualified_runtime_and_actual_capabilities(
    db_session, httpx_client, super_admin, monkeypatch
):
    from app.config import settings
    from training_contract import definition, schema_identity
    from training_contract.dataset import SEGMENTATION

    monkeypatch.setattr(
        settings,
        "training_catalog_json",
        {
            "ultralytics": {
                "version": "8.4.177",
                "image_digest": IMAGE,
                "base_models": {
                    "yolo11n-seg": {"sha256": "a" * 64, "filename": "yolo11n-seg.pt"}
                },
            }
        },
    )
    db_session.add(
        TrainingResourceLease(
            resource_id=adapters.resource_id(),
            executor_id="segmentation-container",
            last_seen_at=service.now(),
            runtime={
                "gpu_uuid": GPU,
                "image_digest": IMAGE,
                "frameworks": {"ultralytics": "8.4.177"},
                "executor_protocol": 2,
                "supported_configs": [
                    schema_identity(
                        definition("ultralytics", "yolo11n-seg", version="8.4.177")
                    )
                ],
            },
        )
    )
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/catalog", headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["compatible"] and item["parameter_schemas"][0]["compatible"]
    assert item["capabilities"]["tasks"] == [SEGMENTATION]
    assert item["capabilities"]["model_registration"]
    assert item["base_models"][0]["task"] == SEGMENTATION


async def test_non_managers_cannot_discover_catalog_projects_or_runs(
    db_session, httpx_client, annotator
):
    await create_project(db_session, owner_id=annotator[0].id)
    for path in ("catalog", "projects", "runs"):
        response = await httpx_client.get(
            f"/api/v1/training/{path}", headers=headers(annotator)
        )
        assert response.status_code == 403


async def test_ready_dataset_filter_is_applied_before_pagination(
    db_session, httpx_client, super_admin
):
    project, dataset = await ready_dataset(db_session, super_admin[0])
    for ordinal in range(21):
        db_session.add(
            TrainingDatasetVersion(
                project_id=project.id,
                creator_id=super_admin[0].id,
                request_id=uuid.uuid4(),
                request_digest="a" * 64,
                snapshot_digest="b" * 64,
                name=f"Pending-{ordinal}",
                snapshot={},
                status="pending",
                created_at=service.now() + timedelta(seconds=ordinal),
            )
        )
    await db_session.flush()
    path = f"/api/v1/projects/{project.id}/training-datasets"
    response = await httpx_client.get(path + "?limit=20", headers=headers(super_admin))
    assert response.json()["total"] == 22
    assert all(item["status"] == "pending" for item in response.json()["items"])
    response = await httpx_client.get(
        path + "?status=ready&limit=1", headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == str(dataset.id)
    dataset.status = "pending"
    await db_session.flush()
    response = await httpx_client.get(
        path + "?status=ready", headers=headers(super_admin)
    )
    assert response.json() == {"total": 0, "items": []}


async def test_queue_observes_lost_execution_without_releasing_its_resource(
    db_session, httpx_client, super_admin
):
    project, dataset = await ready_dataset(db_session, super_admin[0])
    run = await add_run(db_session, project, dataset, super_admin[0])
    attempt = await service.new_attempt(db_session, run)
    run.status = attempt.status = "training"
    attempt.heartbeat_at = service.now() - timedelta(hours=1)
    lease = TrainingResourceLease(
        resource_id=adapters.resource_id(), attempt_id=attempt.id
    )
    db_session.add(lease)
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/training/runs?state=failed", headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["run"]["status"] == "lost"
    assert response.json()["counts"]["active"] == 1
    await db_session.refresh(lease)
    assert lease.attempt_id == attempt.id
