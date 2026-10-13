from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.display_id import next_display_id
from app.services.raster_mask_storage import MAX_RLE_OBJECT_BYTES
from app.utils.raster_mask_rle import (
    MAX_IMAGE_MASK_DIMENSION,
    MAX_IMAGE_MASK_PIXELS,
    MAX_MASK_RUNS,
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_project_and_task(
    db,
    *,
    owner_id: uuid.UUID,
    project_enabled: bool,
    region_enabled: bool,
) -> tuple[Project, Task]:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "projects"),
        name=f"mask-capability-{suffix}",
        type_label="图像-分割",
        type_key="image-seg",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=project_enabled,
        tool_bindings={
            "region": {
                "enabled": region_enabled,
                "classes": [{"name": "object", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-MCAP-{suffix}",
        file_name="mask.png",
        file_path="/tmp/mask.png",
        file_type="image",
    )
    db.add(task)
    await db.flush()
    return project, task


@pytest.mark.asyncio
async def test_project_mask_editing_defaults_true_and_allows_opt_out(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    response = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "mask rollout default",
            "type_label": "图像-分割",
            "type_key": "image-seg",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["raster_mask_native_editing_enabled"] is True

    response = await httpx_client.patch(
        f"/api/v1/projects/{created['id']}",
        json={"raster_mask_native_editing_enabled": False},
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["raster_mask_native_editing_enabled"] is False

    source = await db_session.get(Project, uuid.UUID(created["id"]))
    assert source is not None
    assert source.raster_mask_native_editing_enabled is False

    response = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "mask rollout explicit opt out",
            "type_label": "图像-分割",
            "type_key": "image-seg",
            "raster_mask_native_editing_enabled": False,
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["raster_mask_native_editing_enabled"] is False

    response = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "mask rollout clone",
            "type_label": "图像-分割",
            "type_key": "image-seg",
            "source_project_id": created["id"],
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["raster_mask_native_editing_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment_enabled", "project_enabled", "expected_reason", "expected_write"),
    [
        (True, True, "enabled", True),
        (True, False, "project_disabled", False),
        (False, True, "deployment_disabled", False),
        (False, False, "deployment_disabled", False),
    ],
)
async def test_mask_capability_deployment_project_matrix(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
    deployment_enabled,
    project_enabled,
    expected_reason,
    expected_write,
):
    user, token = super_admin
    _, task = await _seed_project_and_task(
        db_session,
        owner_id=user.id,
        project_enabled=project_enabled,
        region_enabled=True,
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", deployment_enabled)

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-capabilities",
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_enabled"] is project_enabled
    assert body["write_enabled"] is expected_write
    assert body["reason"] == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "read_enabled",
        "deployment_enabled",
        "project_enabled",
        "region_enabled",
        "expected_reason",
        "expected_write",
    ),
    [
        (False, True, True, True, "read_disabled", False),
        (True, False, True, True, "deployment_disabled", False),
        (True, True, False, True, "project_disabled", False),
        (True, True, True, False, "region_disabled", False),
        (True, True, True, True, "enabled", True),
    ],
)
async def test_mask_capability_reasons_and_limits(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
    read_enabled,
    deployment_enabled,
    project_enabled,
    region_enabled,
    expected_reason,
    expected_write,
):
    user, token = super_admin
    _, task = await _seed_project_and_task(
        db_session,
        owner_id=user.id,
        project_enabled=project_enabled,
        region_enabled=region_enabled,
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "raster_mask_read_enabled", read_enabled)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", deployment_enabled)

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-capabilities",
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "read_enabled": read_enabled,
        "write_enabled": expected_write,
        "legacy_polygon_commit_enabled": region_enabled,
        "project_enabled": project_enabled,
        "region_enabled": region_enabled,
        "reason": expected_reason,
        "max_dimension": MAX_IMAGE_MASK_DIMENSION,
        "max_pixels": MAX_IMAGE_MASK_PIXELS,
        "max_runs": MAX_MASK_RUNS,
        "max_bytes": MAX_RLE_OBJECT_BYTES,
    }


@pytest.mark.asyncio
async def test_mask_capabilities_honors_task_visibility(
    httpx_client, super_admin, annotator, db_session
):
    owner, _ = super_admin
    _, annotator_token = annotator
    _, task = await _seed_project_and_task(
        db_session,
        owner_id=owner.id,
        project_enabled=True,
        region_enabled=True,
    )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-capabilities",
        headers=_bearer(annotator_token),
    )

    assert response.status_code == 404
    # No membership hides project existence with the canonical project 404.
    assert response.json()["detail"] == "项目不存在"


@pytest.mark.asyncio
async def test_mask_capabilities_marks_completed_task_read_only(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    _, task = await _seed_project_and_task(
        db_session,
        owner_id=user.id,
        project_enabled=True,
        region_enabled=True,
    )
    task.status = "completed"
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/mask-capabilities",
        headers=_bearer(token),
    )

    assert response.status_code == 200
    assert response.json()["read_enabled"] is True
    assert response.json()["write_enabled"] is False
    assert response.json()["legacy_polygon_commit_enabled"] is False
    assert response.json()["reason"] == "task_locked"
