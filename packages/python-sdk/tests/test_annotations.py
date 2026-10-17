import json
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import AAPError, NotFoundError, ValidationError
from ai_annotation.models import Annotation, AnnotationBulkUpdateResult

from .conftest import API

TASK_ID = str(uuid4())
ANN_ID = str(uuid4())
ANN = {
    "id": ANN_ID,
    "task_id": TASK_ID,
    "source": "manual",
    "annotation_type": "bbox",
    "tool_unit_id": "bbox",
    "class_name": "car",
    "geometry": {"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4},
    "is_active": True,
    "version": 1,
    "created_at": "2026-06-11T00:00:00Z",
}


def test_list_annotations(client, respx_mock):
    respx_mock.get(f"{API}/tasks/{TASK_ID}/annotations").mock(
        return_value=httpx.Response(200, json=[ANN])
    )
    anns = client.annotations.list(TASK_ID)
    assert isinstance(anns[0], Annotation)
    assert anns[0].class_name == "car"


def test_create_annotation(client, respx_mock):
    route = respx_mock.post(f"{API}/tasks/{TASK_ID}/annotations").mock(
        return_value=httpx.Response(201, json=ANN)
    )
    geometry = {"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4}
    ann = client.annotations.create(
        TASK_ID, "bbox", geometry, class_name="car", confidence=0.9
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "annotation_type": "bbox",
        "geometry": geometry,
        "class_name": "car",
        "confidence": 0.9,
    }
    assert str(ann.id) == ANN_ID


def test_update_annotation_patch(client, respx_mock):
    route = respx_mock.patch(f"{API}/tasks/{TASK_ID}/annotations/{ANN_ID}").mock(
        return_value=httpx.Response(200, json={**ANN, "class_name": "truck"})
    )
    ann = client.annotations.update(TASK_ID, ANN_ID, class_name="truck", is_hidden=True)
    body = json.loads(route.calls.last.request.content)
    assert body == {"class_name": "truck", "is_hidden": True}
    assert ann.class_name == "truck"


def test_delete_annotation(client, respx_mock):
    route = respx_mock.delete(f"{API}/tasks/{TASK_ID}/annotations/{ANN_ID}").mock(
        return_value=httpx.Response(204)
    )
    client.annotations.delete(TASK_ID, ANN_ID)
    assert route.called


def test_delete_missing_maps_404(client, respx_mock):
    respx_mock.delete(f"{API}/tasks/{TASK_ID}/annotations/{ANN_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "Annotation not found"})
    )
    with pytest.raises(NotFoundError) as ei:
        client.annotations.delete(TASK_ID, ANN_ID)
    assert ei.value.status_code == 404
    assert ei.value.detail == "Annotation not found"


def test_bulk_update_annotations(client, respx_mock):
    second_id = uuid4()
    route = respx_mock.post(f"{API}/annotations/bulk-update").mock(
        return_value=httpx.Response(
            200,
            json={"updated_ids": [ANN_ID, str(second_id)], "updated_count": 2},
        )
    )
    result = client.annotations.bulk_update(
        [ANN_ID, second_id], class_name="truck", is_locked=True
    )
    assert json.loads(route.calls.last.request.content) == {
        "ids": [ANN_ID, str(second_id)],
        "patch": {"class_name": "truck", "is_locked": True},
    }
    assert isinstance(result, AnnotationBulkUpdateResult)
    assert result.updated_count == 2


def test_bulk_update_annotations_validates_patch(client):
    with pytest.raises(ValueError, match="must not be empty"):
        client.annotations.bulk_update([ANN_ID])
    with pytest.raises(ValueError, match="unsupported"):
        client.annotations.bulk_update([ANN_ID], geometry={"type": "bbox"})


def test_bulk_update_annotations_maps_validation_error(client, respx_mock):
    respx_mock.post(f"{API}/annotations/bulk-update").mock(
        return_value=httpx.Response(422, json={"detail": "invalid patch"})
    )
    with pytest.raises(ValidationError):
        client.annotations.bulk_update([ANN_ID], class_name="bad")


def test_import_annotations_multipart_preserves_partial_receipt(
    client, respx_mock, tmp_path
):
    path = tmp_path / "annotations.json"
    path.write_text('{"schema_version":"1.0","tasks":[]}', encoding="utf-8")
    project_id = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{project_id}/annotations/import").mock(
        return_value=httpx.Response(
            200,
            json={
                "imported": 2,
                "skipped": 1,
                "errors": [
                    {"task_match": {"file_name": "missing.jpg"}, "reason": "not found"}
                ],
                "dry_run": True,
            },
        )
    )
    result = client.annotations.import_file(
        project_id, path, dry_run=True, overwrite=True
    )

    request = route.calls.last.request
    assert request.url.params["format"] == "aap_json"
    assert request.url.params["dry_run"] == "true"
    assert b'name="file"' in request.content
    assert b'name="overwrite"' in request.content and b"true" in request.content
    assert result.errors[0].task_match == {"file_name": "missing.jpg"}
    assert result.errors[0].reason == "not found"
    assert result.imported == 2
    assert result.dry_run is True


def test_import_annotations_file_limit_is_checked_before_http(
    client, respx_mock, tmp_path
):
    path = tmp_path / "too-large.json"
    with path.open("wb") as file:
        file.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(AAPError, match="64 MiB"):
        client.annotations.import_file("project-1", path)
    assert not respx_mock.calls
