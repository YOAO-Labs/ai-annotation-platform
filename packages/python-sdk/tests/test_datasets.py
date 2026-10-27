import json
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest

from ai_annotation import (
    DatasetLifecycleCounts,
    DatasetLifecyclePreview,
    DatasetLifecycleResult,
)
from ai_annotation.dataset_operation_models import DatasetImportFromConnectionRequest
from ai_annotation.errors import ConflictError, NotFoundError, PermissionDeniedError
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


def test_dataset_import_reuses_saved_key_after_lost_response(client, respx_mock):
    key = str(uuid4())
    connection_id = str(uuid4())
    job_id = str(uuid4())
    route = respx_mock.post(f"{API}/datasets/{DS_ID}/import-from-connection").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after the server accepted the job"),
            httpx.Response(202, json={"job_id": job_id}),
        ]
    )
    request = DatasetImportFromConnectionRequest(
        connection_id=connection_id,
        source_path="incoming/images",
        recursive=False,
        include_globs=["*.jpg"],
    )

    def call():
        return client.datasets.import_from_connection(
            DS_ID, request, idempotency_key=key
        )

    with pytest.raises(httpx.ReadTimeout):
        call()
    assert str(call().job_id) == job_id
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        key,
        key,
    ]
    assert [json.loads(req.request.content) for req in route.calls] == [
        {
            "connection_id": connection_id,
            "source_path": "incoming/images",
            "recursive": False,
            "include_globs": ["*.jpg"],
        },
        {
            "connection_id": connection_id,
            "source_path": "incoming/images",
            "recursive": False,
            "include_globs": ["*.jpg"],
        },
    ]


def test_dataset_import_rejects_invalid_key_before_request(client, respx_mock):
    route = respx_mock.post(f"{API}/datasets/{DS_ID}/import-from-connection").mock(
        return_value=httpx.Response(202, json={"job_id": str(uuid4())})
    )

    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        client.datasets.import_from_connection(
            DS_ID,
            DatasetImportFromConnectionRequest(connection_id=uuid4()),
            idempotency_key="invalid",
        )

    assert not route.called


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
    phase_keys = [
        init.calls.last.request.headers["Idempotency-Key"],
        complete.calls.last.request.headers["Idempotency-Key"],
    ]
    assert all(str(UUID(key)) == key for key in phase_keys)
    assert phase_keys[0] != phase_keys[1]
    assert isinstance(items[0], UploadedItem)
    assert str(items[0].item_id) == item_id
    assert items[0].file_name == "img.jpg"
    assert progress == [(1, 1, "img.jpg")]


@pytest.mark.parametrize("phase", ["init", "complete"])
def test_upload_phases_reuse_saved_keys_after_uncertain_response(
    client, respx_mock, phase
):
    from ai_annotation import DatasetUploadInitResult, DatasetUploadCompleteResult

    key, item_id = str(uuid4()), str(uuid4())
    if phase == "init":
        url = f"{API}/datasets/{DS_ID}/items/upload-init"
        response = {
            "item_id": item_id,
            "upload_url": "https://objects.example/source?signature=opaque",
            "expires_in": 900,
        }

        def call():
            return client.datasets.upload_init(
                DS_ID, "image.jpg", "image/jpeg", idempotency_key=key
            )

        expected_type = DatasetUploadInitResult
    else:
        url = f"{API}/datasets/{DS_ID}/items/upload-complete/{item_id}"
        response = {"status": "ok", "item_id": item_id, "linked_tasks": 1}

        def call():
            return client.datasets.upload_complete(DS_ID, item_id, idempotency_key=key)

        expected_type = DatasetUploadCompleteResult
    route = respx_mock.post(url).mock(
        side_effect=[
            httpx.ReadTimeout("committed response lost"),
            httpx.Response(200, json=response),
        ]
    )
    with pytest.raises(httpx.ReadTimeout):
        call()
    assert isinstance(call(), expected_type)
    assert [record.request.headers["Idempotency-Key"] for record in route.calls] == [
        key,
        key,
    ]
    assert route.calls[0].request.content == route.calls[1].request.content


@pytest.mark.parametrize("phase", ["init", "complete"])
def test_upload_phase_invalid_key_sends_no_request(client, respx_mock, phase):
    route = respx_mock.post(url__regex=r".*/items/upload-.*").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        if phase == "init":
            client.datasets.upload_init(DS_ID, "image.jpg", idempotency_key="invalid")
        else:
            client.datasets.upload_complete(DS_ID, uuid4(), idempotency_key="invalid")
    assert route.call_count == 0


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
    assert route.calls.last.request.headers.get("Idempotency-Key") is None
    assert isinstance(result, ZipUploadResult)
    assert result.added == 3
    assert result.skipped == 1


def test_ingest_operations_reuse_only_caller_supplied_keys(
    client, respx_mock, tmp_path
):
    key = str(uuid4())
    z = tmp_path / "data.zip"
    z.write_bytes(b"PK\x03\x04fakezip")
    upload = respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-zip").mock(
        return_value=httpx.Response(
            200,
            json={
                "added": 0,
                "deduped": 0,
                "skipped": 0,
                "errors": [],
                "total_in_zip": 0,
                "linked_tasks": 0,
                "scene_inference_notes": [],
            },
        )
    )
    scan = respx_mock.post(f"{API}/datasets/{DS_ID}/items/scan").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "new_items": 0, "linked_tasks": 0}
        )
    )

    client.datasets.upload_zip(DS_ID, z, idempotency_key=key)
    client.datasets.upload_zip(DS_ID, z, idempotency_key=key)
    client.datasets.scan_items(DS_ID, idempotency_key=key)

    assert [call.request.headers["Idempotency-Key"] for call in upload.calls] == [
        key,
        key,
    ]
    assert scan.calls.last.request.headers["Idempotency-Key"] == key
    assert all(b"fakezip" in call.request.content for call in upload.calls)


def test_ingest_invalid_idempotency_key_sends_no_request(client, respx_mock, tmp_path):
    z = tmp_path / "data.zip"
    z.write_bytes(b"PK\x03\x04fakezip")
    upload = respx_mock.post(f"{API}/datasets/{DS_ID}/items/upload-zip").mock(
        return_value=httpx.Response(200, json={})
    )
    scan = respx_mock.post(f"{API}/datasets/{DS_ID}/items/scan").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        client.datasets.upload_zip(DS_ID, z, idempotency_key="invalid")
    with pytest.raises(ValueError, match="idempotency_key must be a UUID"):
        client.datasets.scan_items(DS_ID, idempotency_key="invalid")

    assert upload.call_count == 0
    assert scan.call_count == 0


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
    operation_id = str(uuid4())
    dimensions_job_id = str(uuid4())
    accepted_before = "2026-10-18T12:34:56Z"
    dimension_key = str(uuid4())
    dimensions = respx_mock.post(f"{API}/datasets/{DS_ID}/backfill-dimensions").mock(
        return_value=httpx.Response(
            202,
            json={
                "dataset_id": DS_ID,
                "operation_id": operation_id,
                "job_id": dimensions_job_id,
                "purpose": "dimensions",
                "status": "pending",
                "accepted_before": accepted_before,
            },
        )
    )
    result = client.datasets.backfill_dimensions(
        DS_ID, batch=25, idempotency_key=dimension_key
    )
    assert result.job_id == UUID(dimensions_job_id)
    assert result.operation_id == UUID(operation_id)
    assert result.purpose == "dimensions"
    assert result.status == "pending"
    assert result.accepted_before.isoformat() == "2026-10-18T12:34:56+00:00"
    assert dimensions.calls.last.request.url.params["batch"] == "25"
    assert dimensions.calls.last.request.headers["Idempotency-Key"] == dimension_key

    media_operation_id = str(uuid4())
    media_job_id = str(uuid4())
    media = respx_mock.post(f"{API}/datasets/{DS_ID}/backfill-media").mock(
        return_value=httpx.Response(
            202,
            json={
                "dataset_id": DS_ID,
                "operation_id": media_operation_id,
                "job_id": media_job_id,
                "purpose": "thumbnail",
                "status": "running",
                "accepted_before": accepted_before,
            },
        )
    )
    media_result = client.datasets.backfill_media(DS_ID)
    assert media_result.job_id == UUID(media_job_id)
    assert media_result.operation_id == UUID(media_operation_id)
    assert media_result.purpose == "thumbnail"
    assert media_result.status == "running"
    generated_key = media.calls.last.request.headers["Idempotency-Key"]
    assert str(UUID(generated_key)) == generated_key
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
    scene_idempotency_key = str(uuid4())
    scene_result = client.datasets.backfill_scenes(
        DS_ID,
        mode="per_subdirectory",
        dry_run=True,
        idempotency_key=scene_idempotency_key,
    )
    scene_params = scenes.calls.last.request.url.params
    assert scene_result.created_scenes == 2
    assert scene_params["mode"] == "per_subdirectory"
    assert scene_params["dry_run"].lower() == "true"
    assert scenes.calls.last.request.headers["Idempotency-Key"] == scene_idempotency_key

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
    assert scan.calls.last.request.headers.get("Idempotency-Key") is None

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


def test_dataset_media_backfill_reuses_key_after_lost_response(client, respx_mock):
    retry_key = str(uuid4())
    retry_job_id = str(uuid4())
    retry_route = respx_mock.post(f"{API}/datasets/{DS_ID}/backfill-media").mock(
        side_effect=[
            httpx.ReadTimeout("response lost after acceptance"),
            httpx.Response(
                202,
                json={
                    "dataset_id": DS_ID,
                    "operation_id": retry_key,
                    "job_id": retry_job_id,
                    "purpose": "thumbnail",
                    "status": "pending",
                    "accepted_before": "2026-10-18T12:34:56Z",
                },
            ),
        ]
    )
    with pytest.raises(httpx.ReadTimeout):
        client.datasets.backfill_media(DS_ID, idempotency_key=retry_key)
    replay = client.datasets.backfill_media(DS_ID, idempotency_key=retry_key)
    assert replay.job_id == UUID(retry_job_id)
    assert [call.request.headers["Idempotency-Key"] for call in retry_route.calls] == [
        retry_key,
        retry_key,
    ]
