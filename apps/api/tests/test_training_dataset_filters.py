"""Filter definitions and project task membership share the frozen snapshot."""

import copy
import json
import uuid

import pytest

from app.db.models.dataset import DatasetItem, ProjectDataset, Scene
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services.storage import storage_service
from tests.test_dataset_views import dataset
from tests.test_training_datasets import (
    reviewed_project,
    source_headers as source_headers,
)
from tests.test_training_dataset_references import (
    reference_training as reference_training,
    build,
    create_version,
)
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_training_immutability import object_bytes
from tests.training_original_fixtures import image_bytes, register_original


@pytest.fixture
async def filtered_project(db_session, super_admin, source_headers):
    db, owner = db_session, super_admin[0]
    project, tasks = await reviewed_project(db, owner)
    ds = await dataset(db, owner)
    scene = Scene(
        dataset_id=ds.id, display_id=f"S-{uuid.uuid4().hex[:12]}", name="Street"
    )
    db.add_all([scene, ProjectDataset(project_id=project.id, dataset_id=ds.id)])
    await db.flush()
    items = []
    for index, task in enumerate(tasks):
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=f"camera-{index}.{'png' if index < 2 else 'jpg'}",
            file_path=task.file_path,
            file_type="image",
            width=1280,
            height=720,
            scene_id=scene.id if index < 2 else None,
        )
        db.add(item)
        await db.flush()
        task.dataset_item_id = item.id
        await register_original(
            db, task, owner, raw=image_bytes(size=(1280, 720)), provider=source_headers
        )
        items.append(item)
    await db.commit()
    return project, ds, scene, tasks, items


def selection(ds, **filters):
    return {"dataset_id": str(ds.id), "dataset_filter": filters}


async def test_filtered_items_task_pages_and_preflight_share_members(
    db_session, filtered_project, httpx_client, super_admin, source_headers
):
    project, ds, scene, tasks, items = filtered_project
    filters = {
        "scene_id": str(scene.id),
        "extension": "png",
        "min_width": 1000,
        "max_height": 800,
        "availability": "unverified",
        "search": "camera",
    }
    auth = headers(super_admin)
    listed = await httpx_client.get(
        f"/api/v1/datasets/{ds.id}/items", params=filters, headers=auth
    )
    assert listed.status_code == 200 and listed.json()["total"] == 2, listed.text
    assert {row["id"] for row in listed.json()["items"]} == {
        str(i.id) for i in items[:2]
    }
    pages = []
    for offset in (0, 1):
        page = await httpx_client.get(
            "/api/v1/tasks",
            params={
                "project_id": str(project.id),
                "dataset_id": str(ds.id),
                "dataset_filter": json.dumps(filters),
                "limit": 1,
                "offset": offset,
            },
            headers=auth,
        )
        assert page.status_code == 200, page.text
        pages.extend(row["id"] for row in page.json()["items"])
        assert page.json()["total"] == (2 if offset == 0 else None)
    assert set(pages) == {str(t.id) for t in tasks[:2]}
    report = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-datasets:preflight",
        json=selection(ds, **filters),
        headers=auth,
    )
    assert report.status_code == 200 and report.json()["ready"], report.text
    assert report.json()["summary"]["selected"] == 2
    assert report.json()["source"]["dataset_selection"]["filters"] == filters
    rejected = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-datasets:preflight",
        json={
            **selection(ds, **filters),
            "mode": "manager_confirmed",
            "task_ids": [str(tasks[2].id)],
        },
        headers=auth,
    )
    assert rejected.status_code == 404


async def test_new_excluded_member_invalidates_preflight_but_created_snapshot_is_fixed(
    db_session, filtered_project, httpx_client, super_admin, source_headers
):
    project, ds, _, tasks, items = filtered_project
    auth, query = headers(super_admin), selection(ds, extension="png")
    url = f"/api/v1/projects/{project.id}/training-datasets"
    report = (
        await httpx_client.post(url + ":preflight", json=query, headers=auth)
    ).json()
    body = {
        **query,
        "name": "Filtered",
        "request_id": str(uuid.uuid4()),
        "preflight_digest": report["digest"],
    }
    tasks[2].status = "pending"
    tasks[2].reviewed_at = None
    items[2].file_name = "camera-new.png"
    await db_session.commit()
    assert (await httpx_client.post(url, json=body, headers=auth)).status_code == 409
    fresh = (
        await httpx_client.post(url + ":preflight", json=query, headers=auth)
    ).json()
    assert (
        fresh["ready"]
        and fresh["summary"]["selected"] == 3
        and fresh["summary"]["eligible"] == 2
    )
    body["preflight_digest"] = fresh["digest"]
    created = await httpx_client.post(url, json=body, headers=auth)
    assert created.status_code == 202, created.text
    version = await db_session.get(
        TrainingDatasetVersion, uuid.UUID(created.json()["id"])
    )
    frozen = copy.deepcopy(version.snapshot)
    assert len(frozen["dataset_selection"]["members"]) == 3
    assert len(frozen["samples"]) == 2
    items[0].file_name = "outside.jpg"
    await db_session.commit()
    retry = await httpx_client.post(url, json=body, headers=auth)
    assert retry.status_code == 202 and retry.json()["id"] == created.json()["id"]
    await db_session.refresh(version)
    assert version.snapshot == frozen


@pytest.mark.parametrize(
    "filters,status",
    [
        ({"scene_id": str(uuid.UUID(int=9))}, 404),
        ({"source_id": str(uuid.UUID(int=9))}, 404),
        ({"min_width": 100, "max_width": 50}, 422),
        ({"unknown": True}, 422),
        (None, 422),
    ],
)
async def test_bad_filters_never_broaden_tasks_or_training(
    filtered_project, httpx_client, super_admin, source_headers, filters, status
):
    project, ds, _, _, _ = filtered_project
    auth = headers(super_admin)
    query = {"dataset_id": str(ds.id), "dataset_filter": filters}
    task_page = await httpx_client.get(
        "/api/v1/tasks",
        params={
            "project_id": str(project.id),
            "dataset_id": str(ds.id),
            "dataset_filter": json.dumps(filters),
        },
        headers=auth,
    )
    assert task_page.status_code == status, task_page.text
    if filters is not None:
        report = await httpx_client.post(
            f"/api/v1/projects/{project.id}/training-datasets:preflight",
            json=query,
            headers=auth,
        )
        assert report.status_code == status, report.text


def test_filter_requires_dataset_and_keeps_legacy_canonical():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TrainingDatasetSelection(dataset_filter={})
    assert (
        TrainingDatasetSelection(dataset_filter=None).canonical_payload()
        == TrainingDatasetSelection().canonical_payload()
    )
    assert TrainingDatasetSelection(
        dataset_id=uuid.uuid4(), dataset_filter={"extension": ".JPEG"}
    ).canonical_payload()["dataset_filter"] == {"extension": "jpg"}


async def test_reference_filter_freezes_real_bytes_and_manifest_after_source_removal(
    db_session, reference_training, httpx_client, super_admin
):
    world, project, _ = reference_training
    version, _ = await create_version(
        db_session,
        httpx_client,
        headers(super_admin),
        project,
        selection(world[3], extension="png", availability="verified"),
    )
    frozen = copy.deepcopy(version.snapshot)
    assert len(frozen["dataset_selection"]["members"]) == 2
    await build(db_session, version)
    assert version.status == "ready", version.error_message
    package = object_bytes(storage_service.training_bucket, version.package_key)
    assert (
        version.manifest["source"]["dataset_selection"]["digest"]
        == frozen["source"]["dataset_selection"]["digest"]
    )
    for path in world[0].iterdir():
        if path.is_file():
            path.unlink()
    assert object_bytes(storage_service.training_bucket, version.package_key) == package
    await db_session.refresh(version)
    assert version.snapshot == frozen


async def test_filtered_task_picker_requires_dataset_scope_and_link(
    filtered_project, httpx_client, super_admin, source_headers
):
    project, ds, _, _, _ = filtered_project
    auth = headers(super_admin)
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=auth,
        json={"name": "Task-only picker", "scopes": ["tasks:read"]},
    )
    assert created.status_code == 201, created.text
    restricted = {"Authorization": "Bearer " + created.json()["plaintext"]}
    params = {
        "project_id": str(project.id),
        "dataset_id": str(ds.id),
        "dataset_filter": "{}",
    }
    result = await httpx_client.get("/api/v1/tasks", params=params, headers=restricted)
    assert result.status_code == 403 and "datasets:read" in result.text
    params.pop("dataset_id")
    assert (
        await httpx_client.get("/api/v1/tasks", params=params, headers=auth)
    ).status_code == 422
    params["dataset_id"] = str(uuid.uuid4())
    assert (
        await httpx_client.get("/api/v1/tasks", params=params, headers=auth)
    ).status_code == 404
