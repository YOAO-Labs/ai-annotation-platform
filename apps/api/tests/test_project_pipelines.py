"""v0.21.0 · 命名项目编排表与 apply 端点."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.project_pipeline import ProjectPipeline
from app.services.ml_backend import MLBackendService
from tests.conftest import create_registry_with_pool


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID, name: str = "pipe"):
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-PIPE-{suffix}",
        name=f"{name}-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        ai_enabled=True,
    )
    db.add(project)
    await db.flush()
    return project


async def _seed_backend(db: AsyncSession, name: str) -> MLBackendRegistry:
    backend, _pool = await create_registry_with_pool(
        db,
        name=name,
        url=f"http://{name}-{uuid.uuid4().hex[:8]}/",
        is_interactive=False,
        state="connected",
    )
    return backend


async def _enable(db: AsyncSession, project: Project, backend: MLBackendRegistry):
    pool = await MLBackendService(db)._pool_for_registry(backend.id)
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    await db.flush()


def _stages(detect_id: uuid.UUID, classify_id: uuid.UUID) -> list[dict]:
    return [
        {"stage": 0, "ml_backend_id": str(detect_id), "model_id": "detect"},
        {
            "stage": 1,
            "ml_backend_id": str(classify_id),
            "model_id": "vehicle-attr",
            "task_type": "classification",
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0.05},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]


@pytest.mark.asyncio
async def test_create_private_pipeline_and_switch_default(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id)
    detect = await _seed_backend(db_session, "detect")
    classify = await _seed_backend(db_session, "classify")
    await _enable(db_session, project, detect)
    await _enable(db_session, project, classify)
    await db_session.commit()

    body = {
        "name": "车辆属性",
        "scope": "private",
        "project_id": str(project.id),
        "stages": _stages(detect.id, classify.id),
        "is_default": True,
    }
    first = await httpx_client.post(
        "/api/v1/project-pipelines", json=body, headers=_auth(token)
    )
    assert first.status_code == 201, first.text
    assert first.json()["is_default"] is True

    second = await httpx_client.post(
        "/api/v1/project-pipelines",
        json={**body, "name": "车辆属性 v2"},
        headers=_auth(token),
    )
    assert second.status_code == 201, second.text
    assert second.json()["is_default"] is True

    rows = await db_session.execute(
        select(ProjectPipeline).where(ProjectPipeline.project_id == project.id)
    )
    defaults = [p for p in rows.scalars().all() if p.is_default]
    assert [p.name for p in defaults] == ["车辆属性 v2"]


@pytest.mark.asyncio
async def test_create_pipeline_does_not_check_backend_enablement(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id)
    missing_backend_id = uuid.uuid4()

    response = await httpx_client.post(
        "/api/v1/project-pipelines",
        json={
            "name": "保存时仅校验结构",
            "scope": "private",
            "project_id": str(project.id),
            "stages": [
                {"stage": 0, "ml_backend_id": str(missing_backend_id)},
            ],
        },
        headers=_auth(token),
    )

    assert response.status_code == 201, response.text
    assert response.json()["stages"][0]["ml_backend_id"] == str(missing_backend_id)


@pytest.mark.asyncio
async def test_update_pipeline_scope_validation_returns_422(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id)
    detect = await _seed_backend(db_session, "detect")
    classify = await _seed_backend(db_session, "classify")
    await _enable(db_session, project, detect)
    await _enable(db_session, project, classify)
    pipeline = ProjectPipeline(
        id=uuid.uuid4(),
        scope="private",
        project_id=project.id,
        name="default",
        stages=_stages(detect.id, classify.id),
        is_default=True,
        created_by=user.id,
    )
    db_session.add(pipeline)
    await db_session.commit()

    resp = await httpx_client.put(
        f"/api/v1/project-pipelines/{pipeline.id}",
        json={"scope": "public"},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_apply_public_pipeline_copies_private_and_bumps_usage(
    httpx_client, project_admin, db_session
):
    user, token = project_admin
    target = await _seed_project(db_session, user.id, name="target")
    detect = await _seed_backend(db_session, "detect")
    classify = await _seed_backend(db_session, "classify")
    await _enable(db_session, target, detect)
    await _enable(db_session, target, classify)

    previous_default = ProjectPipeline(
        id=uuid.uuid4(),
        scope="private",
        project_id=target.id,
        name="previous default",
        stages=_stages(detect.id, classify.id),
        is_default=True,
        created_by=user.id,
    )
    template = ProjectPipeline(
        id=uuid.uuid4(),
        scope="public",
        name="detect-to-attr",
        stages=_stages(detect.id, classify.id),
        created_by=user.id,
    )
    db_session.add_all((template, previous_default))
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{target.id}/pipelines/apply",
        json={"pipeline_id": str(template.id), "set_default": True},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["scope"] == "private"
    assert data["project_id"] == str(target.id)
    assert data["is_default"] is True
    assert data["stages"] == [
        {
            **{k: v for k, v in stage.items() if k != "task_type"},
            **({"task": "classify"} if "task_type" in stage else {}),
        }
        for stage in template.stages
    ]

    defaults = await db_session.execute(
        select(ProjectPipeline).where(
            ProjectPipeline.project_id == target.id,
            ProjectPipeline.is_default.is_(True),
        )
    )
    selected = defaults.scalars().all()
    assert [pipeline.id for pipeline in selected] == [uuid.UUID(data["id"])]
    await db_session.refresh(previous_default)
    assert previous_default.is_default is False

    await db_session.refresh(template)
    assert template.usage_count == 1


@pytest.mark.asyncio
async def test_apply_pipeline_with_unenabled_backend_returns_ids(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    target = await _seed_project(db_session, user.id, name="target")
    detect = await _seed_backend(db_session, "detect")
    classify = await _seed_backend(db_session, "classify")
    await _enable(db_session, target, detect)
    template = ProjectPipeline(
        id=uuid.uuid4(),
        scope="public",
        name="detect-to-attr",
        stages=_stages(detect.id, classify.id),
        created_by=user.id,
    )
    db_session.add(template)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{target.id}/pipelines/apply",
        json={"pipeline_id": str(template.id), "set_default": True},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["unenabled_backends"] == [str(classify.id)]

    await db_session.refresh(template)
    assert template.usage_count == 0
    copies = await db_session.execute(
        select(ProjectPipeline).where(ProjectPipeline.project_id == target.id)
    )
    assert copies.scalars().all() == []


@pytest.mark.asyncio
async def test_read_update_delete_saved_pipeline_and_rejects_invalid_options(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id)
    stages = [
        {"stage": 0, "ml_backend_id": str(uuid.uuid4())},
        {
            "stage": 1,
            "ml_backend_id": str(uuid.uuid4()),
            "parent_stage": 0,
            "roi": {"mode": "crop"},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]
    created = await httpx_client.post(
        "/api/v1/project-pipelines",
        json={
            "name": "pipeline lifecycle",
            "scope": "private",
            "project_id": str(project.id),
            "stages": stages,
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    pipeline_id = created.json()["id"]

    fetched = await httpx_client.get(
        f"/api/v1/project-pipelines/{pipeline_id}", headers=_auth(token)
    )
    assert fetched.status_code == 200, fetched.text
    stored_stages = fetched.json()["stages"]

    invalid = await httpx_client.put(
        f"/api/v1/project-pipelines/{pipeline_id}",
        json={
            "stages": [
                stages[0],
                {**stages[1], "input": {"mode": "full_image"}},
            ]
        },
        headers=_auth(token),
    )
    assert invalid.status_code == 422, invalid.text
    persisted = await db_session.get(ProjectPipeline, uuid.UUID(pipeline_id))
    assert persisted is not None
    await db_session.refresh(persisted)
    assert persisted.stages == stored_stages

    updated = await httpx_client.put(
        f"/api/v1/project-pipelines/{pipeline_id}",
        json={"name": "pipeline lifecycle updated"},
        headers=_auth(token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "pipeline lifecycle updated"
    await db_session.refresh(persisted)
    assert persisted.name == "pipeline lifecycle updated"

    deleted = await httpx_client.delete(
        f"/api/v1/project-pipelines/{pipeline_id}", headers=_auth(token)
    )
    assert deleted.status_code == 204
    assert (
        await db_session.scalar(
            select(ProjectPipeline.id).where(
                ProjectPipeline.id == uuid.UUID(pipeline_id)
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_apply_pipeline_rejects_missing_reference_without_changing_default(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id, name="owned")
    existing_default = ProjectPipeline(
        id=uuid.uuid4(),
        scope="private",
        project_id=project.id,
        name="existing default",
        stages=[{"stage": 0, "ml_backend_id": str(uuid.uuid4())}],
        is_default=True,
        created_by=user.id,
    )
    db_session.add(existing_default)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/pipelines/apply",
        json={"pipeline_id": str(uuid.uuid4()), "set_default": True},
        headers=_auth(token),
    )

    assert response.status_code == 404, response.text
    await db_session.refresh(existing_default)
    assert existing_default.is_default is True
    assert existing_default.name == "existing default"


@pytest.mark.asyncio
async def test_apply_pipeline_rejects_outsider_without_mutation(
    httpx_client, super_admin, project_admin, db_session
):
    owner, _owner_token = super_admin
    outsider, outsider_token = project_admin
    project = await _seed_project(db_session, owner.id, name="owned")
    template = ProjectPipeline(
        id=uuid.uuid4(),
        scope="public",
        name="public template",
        stages=[{"stage": 0, "ml_backend_id": str(uuid.uuid4())}],
        created_by=owner.id,
    )
    db_session.add(template)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/pipelines/apply",
        json={"pipeline_id": str(template.id), "set_default": True},
        headers=_auth(outsider_token),
    )
    assert response.status_code == 404, response.text
    await db_session.refresh(template)
    assert template.usage_count == 0
    copies = await db_session.execute(
        select(ProjectPipeline).where(ProjectPipeline.project_id == project.id)
    )
    assert copies.scalars().all() == []
