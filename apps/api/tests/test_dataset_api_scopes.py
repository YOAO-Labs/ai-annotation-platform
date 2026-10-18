"""Dataset catalog scopes exercise the real ASGI authorization boundary."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from aap_assets_source.ingest_contract import IngestReceipt
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.task import Task
from app.services.storage import storage_service
from tests.factory import create_project


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_key(httpx_client, token: str, scopes: list[str]) -> str:
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=_bearer(token),
        json={"name": "dataset scope test", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    return response.json()["plaintext"]


async def _seed_catalog(db, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:10]

    async def dataset(name: str) -> Dataset:
        row = Dataset(
            display_id=f"DS-SCOPE-{uuid.uuid4().hex[:8]}",
            name=f"{name}-{suffix}",
            data_type="image",
            created_by=owner_id,
        )
        db.add(row)
        await db.flush()
        return row

    async def item(dataset_id: uuid.UUID, name: str) -> DatasetItem:
        row = DatasetItem(
            dataset_id=dataset_id,
            file_name=f"{name}.jpg",
            file_path=f"scope-tests/{suffix}/{name}.jpg",
            file_type="image",
        )
        db.add(row)
        await db.flush()
        return row

    readable = await dataset("readable")
    readable_item = await item(readable.id, "readable")
    project = await create_project(
        db, owner_id=owner_id, name=f"dataset-scope-{suffix}"
    )
    project.data_type = "image"
    association = ProjectDataset(dataset_id=readable.id, project_id=project.id)
    task = Task(
        project_id=project.id,
        dataset_item_id=readable_item.id,
        display_id=f"T-SCOPE-{uuid.uuid4().hex[:8]}",
        file_name=readable_item.file_name,
        file_path=readable_item.file_path,
        file_type="image",
        status="pending",
    )
    db.add_all([association, task])

    deletable = await dataset("deletable")
    item_dataset = await dataset("item-delete")
    deletable_item = await item(item_dataset.id, "delete-me")
    unlinkable = await dataset("unlinkable")
    unlink_item = await item(unlinkable.id, "unlink-me")
    unlink_association = ProjectDataset(dataset_id=unlinkable.id, project_id=project.id)
    unlink_task = Task(
        project_id=project.id,
        dataset_item_id=unlink_item.id,
        display_id=f"T-SCOPE-{uuid.uuid4().hex[:8]}",
        file_name=unlink_item.file_name,
        file_path=unlink_item.file_path,
        file_type="image",
        status="pending",
    )
    db.add_all([unlink_association, unlink_task])
    await db.flush()

    return {
        "readable": readable,
        "readable_item": readable_item,
        "project": project,
        "association": association,
        "task": task,
        "deletable": deletable,
        "item_dataset": item_dataset,
        "deletable_item": deletable_item,
        "unlinkable": unlinkable,
        "unlink_item": unlink_item,
        "unlink_association": unlink_association,
        "unlink_task": unlink_task,
    }


async def _snapshot(db, rows: dict) -> tuple:
    dataset_ids = [
        rows[key].id for key in ("readable", "deletable", "item_dataset", "unlinkable")
    ]
    item_ids = [
        rows[key].id for key in ("readable_item", "deletable_item", "unlink_item")
    ]
    task_ids = [rows[key].id for key in ("task", "unlink_task")]
    datasets = (
        await db.execute(
            select(Dataset.id, Dataset.name, Dataset.description, Dataset.file_count)
            .where(Dataset.id.in_(dataset_ids))
            .order_by(Dataset.id)
        )
    ).all()
    items = (
        await db.execute(
            select(
                DatasetItem.id,
                DatasetItem.dataset_id,
                DatasetItem.file_path,
                DatasetItem.file_type,
            )
            .where(DatasetItem.id.in_(item_ids))
            .order_by(DatasetItem.id)
        )
    ).all()
    associations = (
        await db.execute(
            select(ProjectDataset.dataset_id, ProjectDataset.project_id)
            .where(ProjectDataset.dataset_id.in_(dataset_ids))
            .order_by(ProjectDataset.dataset_id)
        )
    ).all()
    tasks = (
        await db.execute(
            select(Task.id, Task.project_id, Task.dataset_item_id, Task.status)
            .where(Task.id.in_(task_ids))
            .order_by(Task.id)
        )
    ).all()
    job_count = await db.scalar(select(func.count()).select_from(AsyncJob))
    return datasets, items, associations, tasks, job_count


@pytest.mark.asyncio
async def test_read_scope_allows_dataset_reads_and_unlink_preview(
    httpx_client, db_session, super_admin
):
    user, jwt = super_admin
    rows = await _seed_catalog(db_session, user.id)
    token = await _create_key(httpx_client, jwt, ["datasets:read"])
    headers = _bearer(token)
    dataset_id = rows["readable"].id
    project_id = rows["project"].id

    response = await httpx_client.get("/api/v1/datasets", headers=headers)
    assert response.status_code == 200, response.text
    assert any(item["id"] == str(dataset_id) for item in response.json()["items"])

    response = await httpx_client.get(f"/api/v1/datasets/{dataset_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(dataset_id)

    response = await httpx_client.get(
        f"/api/v1/datasets/{dataset_id}/items", headers=headers
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [
        str(rows["readable_item"].id)
    ]

    response = await httpx_client.get(
        f"/api/v1/datasets/{dataset_id}/projects", headers=headers
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [str(project_id)]

    response = await httpx_client.get(
        f"/api/v1/datasets/{dataset_id}/link/{project_id}/preview-unlink",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["will_delete_tasks"] == 1

    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset_id}/sniff-axis-convention", headers=headers
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_read_scope_rejects_every_dataset_mutation_without_work(
    httpx_client, db_session, super_admin, media_dispatcher, monkeypatch
):
    user, jwt = super_admin
    rows = await _seed_catalog(db_session, user.id)
    token = await _create_key(httpx_client, jwt, ["datasets:read"])
    headers = _bearer(token)

    storage_calls: list[str] = []

    def record_storage_call(name):
        def called(*args, **kwargs):
            storage_calls.append(name)
            return None

        return called

    monkeypatch.setattr(
        storage_service, "verify_upload", record_storage_call("verify_upload")
    )
    monkeypatch.setattr(
        storage_service,
        "read_image_dimensions",
        record_storage_call("read_image_dimensions"),
    )

    from app.services.assets_import import DatasetImportCapabilities
    from app.workers import media

    queued: list[str] = []
    import_calls: list[str] = []

    async def record_import_call(self, *_args, **_kwargs):
        import_calls.append("called")

    def record_queue_call(name):
        def called(*args, **kwargs):
            queued.append(name)
            return None

        return called

    monkeypatch.setattr(
        media.backfill_media, "delay", record_queue_call("media-backfill")
    )
    monkeypatch.setattr(DatasetImportCapabilities, "prepare", record_import_call)
    monkeypatch.setattr(DatasetImportCapabilities, "accept", record_import_call)

    async def unexpected_scan(self, command):
        raise AssertionError("dataset scan ran without datasets:write")

    monkeypatch.setattr(
        "app.services.assets_source.AssetsSourceGateway.ingest_scan", unexpected_scan
    )

    project_id = rows["project"].id
    write_endpoints = [
        ("post", "/api/v1/datasets"),
        ("put", f"/api/v1/datasets/{rows['readable'].id}"),
        ("delete", f"/api/v1/datasets/{rows['deletable'].id}"),
        (
            "post",
            f"/api/v1/datasets/{rows['readable'].id}/import-from-connection",
        ),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/items/upload-init"),
        (
            "post",
            f"/api/v1/datasets/{rows['item_dataset'].id}/items/upload-complete/"
            f"{rows['deletable_item'].id}",
        ),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/items/upload-zip"),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/items/scan"),
        (
            "post",
            f"/api/v1/datasets/{rows['item_dataset'].id}/backfill-dimensions",
        ),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/backfill-media"),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/scenes/backfill"),
        (
            "delete",
            f"/api/v1/datasets/{rows['item_dataset'].id}/items/"
            f"{rows['deletable_item'].id}",
        ),
        ("post", f"/api/v1/datasets/{rows['readable'].id}/link"),
        (
            "delete",
            f"/api/v1/datasets/{rows['unlinkable'].id}/link/{project_id}",
        ),
    ]

    before = await _snapshot(db_session, rows)
    dispatch_before = {
        name: list(getattr(media_dispatcher, name))
        for name in (
            "thumbnails",
            "task_thumbnails",
            "task_thumbnail_backfills",
        )
    }
    for method, path in write_endpoints:
        if path.endswith("/items/upload-zip"):
            response = await httpx_client.post(
                path,
                headers={
                    **headers,
                    "Content-Type": "multipart/form-data; boundary=malformed",
                },
                content=b"not a multipart body",
            )
        else:
            response = await getattr(httpx_client, method)(path, headers=headers)
        assert response.status_code == 403, f"{method.upper()} {path}: {response.text}"
        assert response.json()["detail"] == ("API key 缺少所需权限: datasets:write")
        assert response.headers["www-authenticate"] == 'Bearer scope="datasets:write"'

    assert await _snapshot(db_session, rows) == before
    assert {
        name: list(getattr(media_dispatcher, name)) for name in dispatch_before
    } == dispatch_before
    assert storage_calls == []
    assert import_calls == []
    assert queued == []


@pytest.mark.asyncio
async def test_write_scope_is_separate_from_read_and_jwt_wildcard_stay_full_access(
    httpx_client, db_session, super_admin
):
    user, jwt = super_admin
    rows = await _seed_catalog(db_session, user.id)
    dataset_id = rows["readable"].id

    scoped_write = await _create_key(httpx_client, jwt, ["datasets:write"])
    response = await httpx_client.put(
        f"/api/v1/datasets/{dataset_id}",
        headers=_bearer(scoped_write),
        json={"description": "changed by write scope"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "changed by write scope"

    response = await httpx_client.get(
        f"/api/v1/datasets/{dataset_id}", headers=_bearer(scoped_write)
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "API key 缺少所需权限: datasets:read"

    wildcard = await _create_key(httpx_client, jwt, ["*"])
    response = await httpx_client.put(
        f"/api/v1/datasets/{dataset_id}",
        headers=_bearer(wildcard),
        json={"description": "changed by wildcard"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "changed by wildcard"

    response = await httpx_client.put(
        f"/api/v1/datasets/{dataset_id}",
        headers=_bearer(jwt),
        json={"description": "changed by JWT"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "changed by JWT"


@pytest.mark.asyncio
async def test_zip_upload_is_available_to_active_non_manager(
    httpx_client, super_admin, annotator, monkeypatch
):
    _, owner_token = super_admin
    actor, actor_token = annotator
    dataset = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(owner_token),
        json={"name": f"annotator-zip-{uuid.uuid4().hex[:8]}", "data_type": "image"},
    )
    assert dataset.status_code == 201, dataset.text
    command_data = []

    class Gateway:
        is_remote = True

        async def ingest_zip(self, command, source):
            command_data.append((command, source.read()))
            return IngestReceipt(
                operation_id=command.operation_id,
                actor_id=command.actor_id,
                dataset_id=command.dataset_id,
                kind="zip",
                request_digest="a" * 64,
                added=0,
                deduped=0,
                skipped=0,
                errors=[],
                error_count=0,
                total_in_zip=0,
                linked_tasks=0,
                scene_inference_notes=[],
            )

    monkeypatch.setattr(
        "app.api.v1.datasets.AssetsSourceGateway", lambda _db: Gateway()
    )
    operation_id = uuid.uuid4()
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.json()['id']}/items/upload-zip",
        headers={**_bearer(actor_token), "Idempotency-Key": str(operation_id)},
        files={"file": ("empty.zip", b"zip", "application/zip")},
    )

    assert response.status_code == 200, response.text
    assert response.headers["idempotency-key"] == str(operation_id)
    assert command_data[0][0].actor_id == actor.id
    assert command_data[0][0].dataset_id == uuid.UUID(dataset.json()["id"])
    assert command_data[0][1] == b"zip"


@pytest.mark.asyncio
async def test_scan_requires_platform_manager_even_with_dataset_write_scope(
    httpx_client, super_admin, annotator, monkeypatch
):
    _, owner_token = super_admin
    _, actor_token = annotator
    dataset = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(owner_token),
        json={"name": f"manager-scan-{uuid.uuid4().hex[:8]}", "data_type": "image"},
    )
    assert dataset.status_code == 201, dataset.text

    async def unexpected_scan(self, command):
        raise AssertionError("non-manager reached the scan workflow")

    monkeypatch.setattr(
        "app.services.assets_source.AssetsSourceGateway.ingest_scan", unexpected_scan
    )
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.json()['id']}/items/scan",
        headers=_bearer(actor_token),
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_inactive_api_key_owner_cannot_use_dataset_write_scope(
    httpx_client, db_session, super_admin
):
    user, jwt = super_admin
    rows = await _seed_catalog(db_session, user.id)
    token = await _create_key(httpx_client, jwt, ["datasets:write"])
    user.is_active = False
    await db_session.flush()

    response = await httpx_client.put(
        f"/api/v1/datasets/{rows['readable'].id}",
        headers=_bearer(token),
        json={"description": "must not persist"},
    )

    assert response.status_code == 401, response.text
    await db_session.refresh(rows["readable"])
    assert rows["readable"].description == ""
