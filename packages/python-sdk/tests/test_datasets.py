import json
from datetime import datetime
from uuid import uuid4

import httpx
import pytest

from ai_annotation import (
    DatasetLifecycleCounts,
    DatasetLifecyclePreview,
    DatasetLifecycleResult,
)
from ai_annotation.errors import ConflictError, PermissionDeniedError
from ai_annotation.models import (
    Dataset,
    DatasetItem,
    DatasetUnlinkPreview,
    DatasetUnlinkResult,
    LinkResult,
    Page,
    UploadedItem,
    ZipUploadResult,
)

from .conftest import API

DS_ID = str(uuid4())
DATASET = {
    "id": DS_ID,
    "display_id": "D-1",
    "name": "ds",
    "description": "",
    "data_type": "image",
    "file_count": 2,
    "total_size": 100,
    "created_at": "2026-06-11T00:00:00Z",
}


def test_list_datasets(client, respx_mock):
    route = respx_mock.get(f"{API}/datasets").mock(
        return_value=httpx.Response(
            200, json={"items": [DATASET], "total": 1, "limit": 10, "offset": 0}
        )
    )
    page = client.datasets.list(search="ds", limit=10, offset=0)
    req = route.calls.last.request
    assert req.url.params["search"] == "ds"
    assert req.url.params["limit"] == "10"
    assert req.url.params["state"] == "active"
    assert isinstance(page, Page)
    assert isinstance(page.items[0], Dataset)
    assert page.total == 1


@pytest.mark.parametrize("state", ["active", "archived", "all"])
def test_list_datasets_state_preserves_existing_positional_arguments(
    client, respx_mock, state
):
    route = respx_mock.get(f"{API}/datasets").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 10, "offset": 20}
        )
    )
    client.datasets.list("ds", "image", 10, 20, state=state)
    assert dict(route.calls.last.request.url.params) == {
        "search": "ds",
        "data_type": "image",
        "limit": "10",
        "offset": "20",
        "state": state,
    }


def test_dataset_lifecycle_fields_are_typed_and_backward_compatible(client, respx_mock):
    archived_by = str(uuid4())
    route = respx_mock.get(f"{API}/datasets/{DS_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                **DATASET,
                "archived_at": "2026-10-24T01:00:00Z",
                "archived_by": archived_by,
                "lifecycle_revision": 7,
            },
        )
    )
    dataset = client.datasets.get(DS_ID)
    assert isinstance(dataset.archived_at, datetime)
    assert str(dataset.archived_by) == archived_by
    assert dataset.lifecycle_revision == 7
    assert route.called

    active = Dataset.model_validate(DATASET)
    assert active.archived_at is None
    assert active.archived_by is None
    assert active.lifecycle_revision == 1


@pytest.mark.parametrize("operation", ["archive", "restore"])
def test_dataset_lifecycle_preview_and_apply_exact_review(
    client, respx_mock, operation
):
    counts = {
        "items": 12,
        "sources": 2,
        "projects": 3,
        "tasks": 15,
        "annotations": 18,
        "active_scans": 0,
        "active_registrations": 0,
        "enabled_schedules": 1,
        "pending_jobs": 0,
        "ready_lineage_versions": 4,
        "unfinished_lineage_versions": 0,
    }
    preview_route = respx_mock.post(f"{API}/datasets/{DS_ID}/lifecycle-preview").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_id": DS_ID,
                "lifecycle_revision": 7,
                "state": "active" if operation == "archive" else "archived",
                "operation": operation,
                "measured_at": "2026-10-24T01:00:00Z",
                "expires_at": "2026-10-24T01:05:00Z",
                "impact_token": "reviewed-token",
                "counts": counts,
                "frozen_coverage": "recorded_dataset_membership_only",
            },
        )
    )
    preview = client.datasets.preview_lifecycle(DS_ID, operation, expected_revision=7)
    assert isinstance(preview, DatasetLifecyclePreview)
    assert isinstance(preview.counts, DatasetLifecycleCounts)
    assert isinstance(preview.expires_at, datetime)
    assert preview.counts.model_dump() == counts
    assert json.loads(preview_route.calls.last.request.content) == {
        "operation": operation,
        "expected_revision": 7,
    }

    archived_by = str(uuid4())
    apply_route = respx_mock.post(f"{API}/datasets/{DS_ID}/lifecycle").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_id": DS_ID,
                "lifecycle_revision": 8,
                "state": "archived" if operation == "archive" else "active",
                "archived_at": "2026-10-24T01:01:00Z"
                if operation == "archive"
                else None,
                "archived_by": archived_by if operation == "archive" else None,
            },
        )
    )
    result = client.datasets.set_lifecycle(
        preview.dataset_id,
        preview.operation,
        expected_revision=preview.lifecycle_revision,
        impact_token=preview.impact_token,
    )
    assert isinstance(result, DatasetLifecycleResult)
    assert result.lifecycle_revision == 8
    assert json.loads(apply_route.calls.last.request.content) == {
        "operation": operation,
        "expected_revision": 7,
        "impact_token": "reviewed-token",
    }
    assert preview_route.call_count == apply_route.call_count == 1


@pytest.mark.parametrize("status", [403, 409])
def test_set_lifecycle_does_not_refresh_review_or_retry(client, respx_mock, status):
    detail = {"code": "dataset_lifecycle_preview_changed", "message": "Review again"}
    route = respx_mock.post(f"{API}/datasets/{DS_ID}/lifecycle").mock(
        return_value=httpx.Response(status, json={"detail": detail})
    )
    error_type = PermissionDeniedError if status == 403 else ConflictError
    with pytest.raises(error_type) as exc:
        client.datasets.set_lifecycle(
            DS_ID, "archive", expected_revision=7, impact_token="old-token"
        )
    assert exc.value.detail == detail
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1


def test_create_and_get_dataset(client, respx_mock):
    create = respx_mock.post(f"{API}/datasets").mock(
        return_value=httpx.Response(201, json=DATASET)
    )
    ds = client.datasets.create(name="ds", data_type="image", is_temporal=False)
    body = json.loads(create.calls.last.request.content)
    assert body == {"name": "ds", "data_type": "image", "is_temporal": False}
    assert ds.display_id == "D-1"

    respx_mock.get(f"{API}/datasets/{DS_ID}").mock(
        return_value=httpx.Response(200, json=DATASET)
    )
    assert client.datasets.get(DS_ID).name == "ds"


def test_update_dataset_preserves_explicit_none(client, respx_mock):
    route = respx_mock.put(f"{API}/datasets/{DS_ID}").mock(
        return_value=httpx.Response(200, json=DATASET)
    )
    dataset = client.datasets.update(DS_ID, axis_convention=None)
    assert json.loads(route.calls.last.request.content) == {"axis_convention": None}
    assert isinstance(dataset, Dataset)


def test_delete_dataset(client, respx_mock):
    route = respx_mock.delete(f"{API}/datasets/{DS_ID}").mock(
        return_value=httpx.Response(204)
    )
    assert client.datasets.delete(DS_ID) is None
    assert route.called


def test_list_and_delete_dataset_items(client, respx_mock):
    item_id = str(uuid4())
    list_route = respx_mock.get(f"{API}/datasets/{DS_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": item_id,
                        "dataset_id": DS_ID,
                        "file_name": "a.jpg",
                        "file_path": "datasets/a.jpg",
                        "file_type": "image/jpeg",
                    }
                ],
                "total": 1,
                "limit": 10,
                "offset": 20,
            },
        )
    )
    page = client.datasets.list_items(DS_ID, limit=10, offset=20)
    params = list_route.calls.last.request.url.params
    assert params["limit"] == "10"
    assert params["offset"] == "20"
    assert isinstance(page.items[0], DatasetItem)

    delete_route = respx_mock.delete(f"{API}/datasets/{DS_ID}/items/{item_id}").mock(
        return_value=httpx.Response(204)
    )
    assert client.datasets.delete_item(DS_ID, item_id) is None
    assert delete_route.called


def test_list_projects_and_unlink(client, respx_mock):
    project_id = str(uuid4())
    project = {
        "id": project_id,
        "display_id": "P-2",
        "name": "linked",
        "type_key": "object_detection",
        "data_type": "image",
        "status": "active",
    }
    respx_mock.get(f"{API}/datasets/{DS_ID}/projects").mock(
        return_value=httpx.Response(200, json=[project])
    )
    assert client.datasets.list_projects(DS_ID)[0].display_id == "P-2"

    preview_route = respx_mock.get(
        f"{API}/datasets/{DS_ID}/link/{project_id}/preview-unlink"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "will_delete_tasks": 4,
                "will_delete_annotations": 8,
                "will_delete_batches": 1,
            },
        )
    )
    preview = client.datasets.preview_unlink(DS_ID, project_id)
    assert preview_route.called
    assert isinstance(preview, DatasetUnlinkPreview)
    assert preview.will_delete_annotations == 8

    batch_id = str(uuid4())
    unlink_route = respx_mock.delete(f"{API}/datasets/{DS_ID}/link/{project_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "deleted_tasks": 4,
                "deleted_annotations": 8,
                "deleted_batches": 1,
                "deleted_batch_ids": [batch_id],
            },
        )
    )
    result = client.datasets.unlink_project(DS_ID, project_id)
    assert unlink_route.called
    assert isinstance(result, DatasetUnlinkResult)
    assert result.deleted_batches == 1
    assert str(result.deleted_batch_ids[0]) == batch_id


@pytest.mark.parametrize(
    ("http_method", "path", "call"),
    [
        ("put", f"/datasets/{DS_ID}", lambda c: c.datasets.update(DS_ID, name="x")),
        ("delete", f"/datasets/{DS_ID}", lambda c: c.datasets.delete(DS_ID)),
        (
            "get",
            f"/datasets/{DS_ID}/items",
            lambda c: c.datasets.list_items(DS_ID),
        ),
        (
            "delete",
            f"/datasets/{DS_ID}/items/item",
            lambda c: c.datasets.delete_item(DS_ID, "item"),
        ),
        (
            "get",
            f"/datasets/{DS_ID}/projects",
            lambda c: c.datasets.list_projects(DS_ID),
        ),
        (
            "get",
            f"/datasets/{DS_ID}/link/project/preview-unlink",
            lambda c: c.datasets.preview_unlink(DS_ID, "project"),
        ),
        (
            "delete",
            f"/datasets/{DS_ID}/link/project",
            lambda c: c.datasets.unlink_project(DS_ID, "project"),
        ),
    ],
)
def test_dataset_management_maps_permission_error(
    client, respx_mock, http_method, path, call
):
    getattr(respx_mock, http_method)(f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        call(client)


def test_upload_files_three_step_flow(client, respx_mock, tmp_path):
    item_id = str(uuid4())
    f = tmp_path / "img.jpg"
    f.write_bytes(b"jpegdata")

    init = respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-init").mock(
        return_value=httpx.Response(
            200,
            json={
                "item_id": item_id,
                "upload_url": "http://minio.local/bucket/img.jpg?sig=1",
                "expires_in": 900,
            },
        )
    )
    put = respx_mock.put(host="minio.local", path="/bucket/img.jpg").mock(
        return_value=httpx.Response(200)
    )
    complete = respx_mock.post(
        f"{API}/datasets/{DS_ID}/items/upload-complete/{item_id}"
    ).mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "item_id": item_id, "linked_tasks": 1}
        )
    )

    progress = []
    items = client.datasets.upload_files(
        DS_ID,
        [f],
        on_progress=lambda done, total, name: progress.append((done, total, name)),
    )

    init_body = json.loads(init.calls.last.request.content)
    assert init_body == {"file_name": "img.jpg", "content_type": "image/jpeg"}
    put_req = put.calls.last.request
    assert put_req.content == b"jpegdata"
    assert put_req.headers["Content-Type"] == "image/jpeg"
    # 预签名 PUT 不应携带平台 auth
    assert "Authorization" not in put_req.headers
    assert complete.called
    assert isinstance(items[0], UploadedItem)
    assert str(items[0].item_id) == item_id
    assert items[0].file_name == "img.jpg"
    assert progress == [(1, 1, "img.jpg")]


def test_upload_zip(client, respx_mock, tmp_path):
    z = tmp_path / "data.zip"
    z.write_bytes(b"PK\x03\x04fakezip")
    route = respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-zip").mock(
        return_value=httpx.Response(
            200,
            json={
                "added": 3,
                "deduped": 0,
                "skipped": 1,
                "errors": [],
                "total_in_zip": 4,
                "linked_tasks": 3,
                "scene_inference_notes": [],
            },
        )
    )
    result = client.datasets.upload_zip(DS_ID, z)
    content = route.calls.last.request.content
    assert b'name="file"' in content
    assert b'filename="data.zip"' in content
    assert isinstance(result, ZipUploadResult)
    assert result.added == 3
    assert result.skipped == 1


def test_link_project_async(client, respx_mock):
    pid = str(uuid4())
    job_id = str(uuid4())
    route = respx_mock.post(f"{API}/datasets/{DS_ID}/link").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "linking",
                "dataset_id": DS_ID,
                "project_id": pid,
                "async_job_id": job_id,
            },
        )
    )
    result = client.datasets.link_project(DS_ID, pid)
    assert json.loads(route.calls.last.request.content) == {"project_id": pid}
    assert isinstance(result, LinkResult)
    assert result.status == "linking"
    assert str(result.async_job_id) == job_id
