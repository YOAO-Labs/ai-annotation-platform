"""Image candidate pixels retain original shape identity and current read authority."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from tests.test_image_raster_mask_api import (
    EMPTY_RLE,
    FOREGROUND_RLE,
    _headers,
    _seed_image_mask,
)


async def _seed(db, user):
    task, annotation = await _seed_image_mask(db, owner_id=user.id, user_id=user.id)
    prediction = Prediction(
        task_id=task.id,
        project_id=task.project_id,
        tool_unit_id="region",
        result=[
            {
                "type": "bbox",
                "class_name": "object",
                "confidence": 0.1,
                "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
            },
            {
                "type": "raster_mask",
                "class_name": "object",
                "confidence": 0.9,
                "geometry": annotation.geometry,
            },
        ],
    )
    db.add(prediction)
    await db.flush()
    return task, prediction, annotation


def _url(task, prediction, index=1):
    return f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/mask-content/{index}"


def test_image_candidate_content_openapi():
    from app.main import app

    operation = app.openapi()["paths"][
        "/api/v1/tasks/{task_id}/predictions/{prediction_id}/mask-content/{shape_index}"
    ]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CocoRleContent"
    }
    assert set(operation["responses"]) >= {"200", "304", "409", "503"}


async def test_candidate_uses_original_index_and_reads_independently_of_write_gates(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, prediction, annotation = await _seed(db_session, user)
    project = await db_session.get(Project, task.project_id)
    project.raster_mask_native_editing_enabled = False
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    project.ai_enabled = False
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", load)
    await db_session.commit()

    listed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions?min_confidence=0.8",
        headers=_headers(token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["result"][0]["shape_index"] == 1
    assert listed.json()[0]["result"][0]["tool_unit_id"] == "region"
    first = await httpx_client.get(_url(task, prediction), headers=_headers(token))
    assert first.status_code == 200, first.text
    assert first.json() == FOREGROUND_RLE
    assert first.headers["cache-control"] == "private, no-cache"
    etag = first.headers["etag"]
    for value in (etag, f"W/{etag}", f'"other", W/{etag}', "*"):
        cached = await httpx_client.get(
            _url(task, prediction), headers=_headers(token, **{"If-None-Match": value})
        )
        assert cached.status_code == 304
        assert cached.headers["etag"] == etag
    assert load.await_count == 5
    assert all(
        call.args == (prediction.result[1]["geometry"]["mask"],)
        for call in load.await_args_list
    )

    # Editing an accepted annotation never changes the original candidate's pixels.
    from app.services.raster_mask_storage import build_rle_reference

    annotation.parent_prediction_id = prediction.id
    annotation.geometry = {
        "type": "raster_mask",
        "mask": build_rle_reference(EMPTY_RLE),
    }
    await db_session.commit()
    original = await httpx_client.get(_url(task, prediction), headers=_headers(token))
    assert original.status_code == 200
    assert original.json() == FOREGROUND_RLE


@pytest.mark.parametrize(
    "change,expected",
    [
        ("rejected", 404),
        ("interactive", 404),
        ("wrong_task", 404),
        ("wrong_project", 404),
        ("missing", 404),
        ("out_of_range", 404),
        ("vector", 422),
        ("read_disabled", 404),
        ("task_video", 409),
        ("item_video", 409),
        ("dimensions", 409),
    ],
)
async def test_current_context_checked_before_conditional_response(
    httpx_client, db_session, super_admin, monkeypatch, change, expected
):
    user, token = super_admin
    task, prediction, _ = await _seed(db_session, user)
    index = 1
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    if change == "rejected":
        prediction.rejected_shape_indexes = [1]
    elif change == "interactive":
        prediction.source = "interactive_accept"
    elif change in {"wrong_task", "wrong_project"}:
        other, _, _ = await _seed(db_session, user)
        if change == "wrong_task":
            prediction.task_id = other.id
        else:
            prediction.project_id = other.project_id
    elif change == "out_of_range":
        index = 99
    elif change == "vector":
        index = 0
    elif change == "read_disabled":
        monkeypatch.setattr(settings, "raster_mask_read_enabled", False)
    elif change == "task_video":
        task.file_type = "video"
    elif change in {"item_video", "dimensions"}:
        item = await db_session.get(DatasetItem, task.dataset_item_id)
        if change == "item_video":
            item.file_type = "video"
        else:
            item.width = 30
    await db_session.commit()
    url = _url(task, prediction, index)
    if change == "missing":
        url = url.replace(str(prediction.id), str(uuid.uuid4()))
    load = AsyncMock()
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", load)
    response = await httpx_client.get(
        url, headers=_headers(token, **{"If-None-Match": "*"})
    )
    assert response.status_code == expected, response.text
    load.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (ValueError("stored mask RLE digest mismatch"), 409, "digest_mismatch"),
        (RuntimeError("private upstream credentials"), 503, "mask_storage_unavailable"),
    ],
)
async def test_candidate_storage_errors_are_retryable(
    httpx_client, db_session, super_admin, monkeypatch, error, status, reason
):
    user, token = super_admin
    task, prediction, _ = await _seed(db_session, user)
    await db_session.commit()
    monkeypatch.setattr(
        "app.api.v1.tasks.predictions.load_coco_rle", AsyncMock(side_effect=error)
    )
    response = await httpx_client.get(_url(task, prediction), headers=_headers(token))
    assert response.status_code == status
    assert response.json()["detail"]["reason"] == reason
    assert response.json()["detail"]["retryable"] is True
    assert "private upstream" not in response.text


async def test_candidate_pixels_require_current_task_visibility(
    httpx_client, db_session, super_admin, annotator, monkeypatch
):
    owner, _ = super_admin
    _, token = annotator
    task, prediction, _ = await _seed(db_session, owner)
    await db_session.commit()
    load = AsyncMock()
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", load)
    response = await httpx_client.get(
        _url(task, prediction), headers=_headers(token, **{"If-None-Match": "*"})
    )
    assert response.status_code == 404
    load.assert_not_awaited()


async def test_candidate_requires_annotation_read_key_scope(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, prediction, _ = await _seed(db_session, user)
    await db_session.commit()
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", load)
    for scopes, expected in [(["datasets:read"], 403), (["annotations:read"], 200)]:
        created = await httpx_client.post(
            "/api/v1/me/api-keys",
            headers=_headers(token),
            json={"name": "mask reader", "scopes": scopes},
        )
        assert created.status_code == 201, created.text
        response = await httpx_client.get(
            _url(task, prediction), headers=_headers(created.json()["plaintext"])
        )
        assert response.status_code == expected, response.text
    load.assert_awaited_once()


async def test_legacy_and_mixed_shape_units_are_readable(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    task, prediction, _ = await _seed(db_session, user)
    shapes = prediction.result
    shapes[0]["tool_unit_id"] = "ai_interactive"
    shapes[1]["tool_unit_id"] = "region"
    prediction.result = [dict(shape) for shape in shapes]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(prediction, "result")
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions", headers=_headers(token)
    )
    assert response.status_code == 200, response.text
    assert [shape["tool_unit_id"] for shape in response.json()[0]["result"]] == [
        "bbox",
        "region",
    ]
