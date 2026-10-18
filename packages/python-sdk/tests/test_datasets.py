import json
from uuid import UUID, uuid4

import httpx
import pytest

from ai_annotation.errors import ConflictError, NotFoundError, PermissionDeniedError
from ai_annotation.dataset_operation_models import DatasetImportFromConnectionRequest
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
    assert isinstance(page, Page)
    assert isinstance(page.items[0], Dataset)
    assert page.total == 1


def test_create_and_get_dataset(client, respx_mock):
    create = respx_mock.post(f"{API}/datasets").mock(
        return_value=httpx.Response(201, json=DATASET)
    )
    ds = client.datasets.create(name="ds", data_type="image", is_temporal=False)
    body = json.loads(create.calls.last.request.content)
    assert body == {"name": "ds", "data_type": "image", "is_temporal": False}
    key = create.calls.last.request.headers["Idempotency-Key"]
    assert str(UUID(key)) == key
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
    key = route.calls.last.request.headers["Idempotency-Key"]
    assert str(UUID(key)) == key
    assert isinstance(dataset, Dataset)


@pytest.mark.parametrize("operation", ["create", "update"])
def test_dataset_create_update_reuses_saved_key_after_lost_response(
    client, respx_mock, operation
):
    key = str(uuid4())
    if operation == "create":
        route = respx_mock.post(f"{API}/datasets").mock(
            side_effect=[
                httpx.ReadTimeout(
                    "response lost after the server accepted the request"
                ),
                httpx.Response(201, json=DATASET),
            ]
        )

        def call():
            return client.datasets.create(
                name="ds", data_type="image", is_temporal=False, idempotency_key=key
            )

        expected_body = {
            "name": "ds",
            "data_type": "image",
            "is_temporal": False,
        }
    else:
        route = respx_mock.put(f"{API}/datasets/{DS_ID}").mock(
            side_effect=[
                httpx.ReadTimeout(
                    "response lost after the server accepted the request"
                ),
                httpx.Response(200, json=DATASET),
            ]
        )

        def call():
            return client.datasets.update(
                DS_ID, axis_convention=None, idempotency_key=key
            )

        expected_body = {"axis_convention": None}

    with pytest.raises(httpx.ReadTimeout):
        call()
    assert isinstance(call(), Dataset)
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        key,
        key,
    ]
    assert [json.loads(call.request.content) for call in route.calls] == [
        expected_body,
        expected_body,
    ]


@pytest.mark.parametrize("operation", ["create", "update"])
def test_invalid_dataset_mutation_key_sends_no_request(client, respx_mock, operation):
    if operation == "create":
        route = respx_mock.post(f"{API}/datasets").mock(
            return_value=httpx.Response(201, json=DATASET)
        )

        def call():
            return client.datasets.create(name="ds", idempotency_key="not-a-uuid")
    else:
        route = respx_mock.put(f"{API}/datasets/{DS_ID}").mock(
            return_value=httpx.Response(200, json=DATASET)
        )

        def call():
            return client.datasets.update(
                DS_ID, name="new name", idempotency_key="not-a-uuid"
            )

    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        call()
    assert route.call_count == 0


def test_delete_dataset(client, respx_mock):
    route = respx_mock.delete(f"{API}/datasets/{DS_ID}").mock(
        return_value=httpx.Response(204)
    )
    assert client.datasets.delete(DS_ID) is None
    assert route.called
    key = route.calls.last.request.headers["Idempotency-Key"]
    assert str(UUID(key)) == key


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
    key = delete_route.calls.last.request.headers["Idempotency-Key"]
    assert str(UUID(key)) == key


@pytest.mark.parametrize(
    ("path", "delete"),
    [
        (
            f"/datasets/{DS_ID}",
            lambda client, key: client.datasets.delete(DS_ID, idempotency_key=key),
        ),
        (
            f"/datasets/{DS_ID}/items/item",
            lambda client, key: client.datasets.delete_item(
                DS_ID, "item", idempotency_key=key
            ),
        ),
    ],
)
def test_source_delete_reuses_saved_key_after_lost_response(
    client, respx_mock, path, delete
):
    key = str(uuid4())
    route = respx_mock.delete(f"{API}{path}").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the request"),
            httpx.Response(204),
        ]
    )

    with pytest.raises(httpx.ReadTimeout):
        delete(client, key)
    assert delete(client, key) is None
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        key,
        key,
    ]


@pytest.mark.parametrize(
    ("status", "detail", "exception_type"),
    [
        (404, "Item not found", NotFoundError),
        (
            409,
            {"code": "source_in_use", "message": "Reference retained the source"},
            ConflictError,
        ),
        (409, {"reason": "source_busy"}, ConflictError),
        (409, {"reason": "operation_conflict"}, ConflictError),
    ],
)
def test_source_delete_preserves_server_error_details(
    client, respx_mock, status, detail, exception_type
):
    route = respx_mock.delete(f"{API}/datasets/{DS_ID}/items/item").mock(
        return_value=httpx.Response(status, json={"detail": detail})
    )
    with pytest.raises(exception_type) as caught:
        client.datasets.delete_item(DS_ID, "item", idempotency_key=str(uuid4()))
    assert caught.value.status_code == status
    assert caught.value.detail == detail
    assert route.called


@pytest.mark.parametrize(
    ("path", "delete"),
    [
        (
            f"/datasets/{DS_ID}",
            lambda client, key: client.datasets.delete(DS_ID, idempotency_key=key),
        ),
        (
            f"/datasets/{DS_ID}/items/item",
            lambda client, key: client.datasets.delete_item(
                DS_ID, "item", idempotency_key=key
            ),
        ),
    ],
)
def test_invalid_source_delete_key_sends_no_request(client, respx_mock, path, delete):
    route = respx_mock.delete(f"{API}{path}").mock(return_value=httpx.Response(204))
    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        delete(client, "not-a-uuid")
    assert route.call_count == 0


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
    assert (
        str(UUID(unlink_route.calls.last.request.headers["Idempotency-Key"]))
        == (unlink_route.calls.last.request.headers["Idempotency-Key"])
    )
    assert isinstance(result, DatasetUnlinkResult)
    assert result.deleted_batches == 1
    assert str(result.deleted_batch_ids[0]) == batch_id


def test_unlink_reuses_persisted_key_after_lost_response(client, respx_mock):
    project_id = str(uuid4())
    idempotency_key = str(uuid4())
    response_body = {
        "deleted_tasks": 1,
        "deleted_annotations": 2,
        "deleted_batches": 0,
        "deleted_batch_ids": [],
        "soft": False,
    }
    route = respx_mock.delete(f"{API}/datasets/{DS_ID}/link/{project_id}").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the request"),
            httpx.Response(200, json=response_body),
        ]
    )

    with pytest.raises(httpx.ReadTimeout):
        client.datasets.unlink_project(
            DS_ID, project_id, idempotency_key=idempotency_key
        )

    result = client.datasets.unlink_project(
        DS_ID, project_id, idempotency_key=idempotency_key
    )

    assert isinstance(result, DatasetUnlinkResult)
    assert result.deleted_tasks == 1
    assert route.call_count == 2
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        idempotency_key,
        idempotency_key,
    ]


def test_invalid_unlink_idempotency_key_sends_no_request(client, respx_mock):
    project_id = str(uuid4())
    route = respx_mock.delete(f"{API}/datasets/{DS_ID}/link/{project_id}").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        client.datasets.unlink_project(DS_ID, project_id, idempotency_key="not-a-uuid")

    assert route.call_count == 0


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


def test_dataset_operation_routes_and_typed_responses(client, respx_mock):
    dimensions = respx_mock.post(f"{API}/datasets/{DS_ID}/backfill-dimensions").mock(
        return_value=httpx.Response(
            200, json={"processed": 10, "failed": 1, "remaining_hint": True}
        )
    )
    result = client.datasets.backfill_dimensions(DS_ID, batch=25)
    assert result.processed == 10
    assert result.failed == 1
    assert result.remaining_hint is True
    assert dimensions.calls.last.request.url.params["batch"] == "25"

    media = respx_mock.post(f"{API}/datasets/{DS_ID}/backfill-media").mock(
        return_value=httpx.Response(200, json={"status": "queued", "dataset_id": DS_ID})
    )
    assert client.datasets.backfill_media(DS_ID).status == "queued"
    assert media.called

    scenes = respx_mock.post(f"{API}/datasets/{DS_ID}/scenes/backfill").mock(
        return_value=httpx.Response(
            200,
            json={
                "dataset_id": DS_ID,
                "created_scenes": 2,
                "assigned_items": 6,
                "skipped_items": 1,
                "dry_run": True,
                "notes": ["inferred by subdirectory"],
            },
        )
    )
    scene_result = client.datasets.backfill_scenes(
        DS_ID, mode="per_subdirectory", dry_run=True
    )
    scene_params = scenes.calls.last.request.url.params
    assert scene_result.created_scenes == 2
    assert scene_params["mode"] == "per_subdirectory"
    assert scene_params["dry_run"].lower() == "true"

    connection_id = str(uuid4())
    job_id = str(uuid4())
    imported = respx_mock.post(f"{API}/datasets/{DS_ID}/import-from-connection").mock(
        return_value=httpx.Response(202, json={"job_id": job_id})
    )
    import_request = DatasetImportFromConnectionRequest(
        connection_id=connection_id,
        source_path="incoming/images",
        recursive=False,
        include_globs=["*.jpg"],
    )
    import_result = client.datasets.import_from_connection(DS_ID, import_request)
    assert str(import_result.job_id) == job_id
    assert json.loads(imported.calls.last.request.content) == {
        "connection_id": connection_id,
        "source_path": "incoming/images",
        "recursive": False,
        "include_globs": ["*.jpg"],
    }

    scan = respx_mock.post(f"{API}/datasets/{DS_ID}/items/scan").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "new_items": 3, "linked_tasks": 2}
        )
    )
    assert client.datasets.scan_items(DS_ID).new_items == 3
    assert scan.called

    camera_item_id = str(uuid4())
    sniff = respx_mock.post(f"{API}/datasets/{DS_ID}/sniff-axis-convention").mock(
        return_value=httpx.Response(
            200,
            json={
                "best": "apollo",
                "score": 0.9,
                "candidates": [{"convention": "apollo", "score": 0.9}],
                "source": "dataset_item",
                "camera_role": "camera_front",
                "camera_item_id": camera_item_id,
                "per_camera": [
                    {"camera_role": "camera_front", "best": "apollo", "score": 0.9}
                ],
                "agreement": 1,
                "future_extension": "preserved",
            },
        )
    )
    sniff_result = client.datasets.sniff_axis_convention(DS_ID)
    assert sniff_result.best == "apollo"
    assert sniff_result.per_camera[0].score == 0.9
    assert sniff_result.future_extension == "preserved"
    assert sniff.called
