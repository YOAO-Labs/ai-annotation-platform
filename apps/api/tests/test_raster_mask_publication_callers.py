"""Publication boundaries preserve caller ownership before conditional responses."""

from __future__ import annotations

import json
import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from app.api.v1._mask_content import mask_content_response
from app.services import predictions_import
from app.services.annotation_conversion import AnnotationConversionService
from app.services.raster_mask_storage import build_rle_reference
from app.workers import mask_qc


RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}


def _conditional_request():
    return Request({"type": "http", "headers": [(b"if-none-match", b"*")]})


async def test_conditional_mask_read_verifies_content_before_304():
    reference = build_rle_reference(RLE)
    loader = AsyncMock(return_value=RLE)

    response = await mask_content_response(
        owner_id=uuid.uuid4(),
        mask_ref=reference,
        request=_conditional_request(),
        loader=loader,
    )

    assert response.status_code == 304
    assert response.headers["etag"] == f'"{reference["sha256"]}"'
    loader.assert_awaited_once_with(reference)


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("raster_object_storage_unpinned", 409, 409),
        ("raster_object_storage_missing", 404, 409),
        ("raster_object_storage_unavailable", 503, 503),
    ],
)
async def test_conditional_mask_read_preserves_owned_rejections(code, status, expected):
    error = ValueError("provider details must not reach the response")
    error.code = code
    error.status_code = status
    loader = AsyncMock(side_effect=error)

    with pytest.raises(HTTPException) as caught:
        await mask_content_response(
            owner_id=uuid.uuid4(),
            mask_ref=build_rle_reference(RLE),
            request=_conditional_request(),
            loader=loader,
        )

    assert caught.value.status_code == expected
    assert caught.value.detail["reason"] == code
    assert "provider details" not in caught.value.detail["message"]
    loader.assert_awaited_once()


async def test_conditional_mask_read_keeps_provider_failures_generic():
    with pytest.raises(HTTPException) as caught:
        await mask_content_response(
            owner_id=uuid.uuid4(),
            mask_ref=build_rle_reference(RLE),
            request=_conditional_request(),
            loader=AsyncMock(side_effect=RuntimeError("private provider details")),
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["reason"] == "mask_storage_unavailable"
    assert "private provider details" not in caught.value.detail["message"]


def _prediction_payload(format):
    if format == "aap":
        reference = build_rle_reference(RLE)
        return json.dumps(
            {
                "schema_version": "1.3",
                "mask_objects": {reference["sha256"]: RLE},
                "tasks": [
                    {
                        "task_match": {"file_path": "image.png"},
                        "media_type": "image",
                        "predictions": [
                            {
                                "class_name": "object",
                                "geometry": {"type": "raster_mask", "mask": reference},
                            }
                        ],
                    }
                ],
            }
        ).encode()
    return json.dumps(
        {
            "images": [{"id": 1, "file_name": "image.png", "width": 3, "height": 2}],
            "categories": [{"id": 1, "name": "object"}],
            "annotations": [{"image_id": 1, "category_id": 1, "segmentation": RLE}],
        }
    ).encode()


@pytest.mark.parametrize("format", ["aap", "coco"])
@pytest.mark.parametrize("busy", [False, True])
async def test_prediction_import_locks_task_before_any_mask_publication(
    monkeypatch, format, busy
):
    task = SimpleNamespace(id=uuid.uuid4())
    project_id = uuid.uuid4()
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))
    events = []

    async def lock(caller, ids, *, nowait):
        assert caller is db and ids == [task.id] and nowait
        events.append("task")
        if busy:
            raise HTTPException(409, {"reason": "task_evidence_busy"})

    async def store(*args, **kwargs):
        assert (args[0] is db) if format == "aap" else (kwargs["db"] is db)
        events.append("store")
        return build_rle_reference(RLE)

    async def create(*args, **kwargs):
        events.append("prediction")

    monkeypatch.setattr(
        predictions_import, "resolve_task", AsyncMock(return_value=task)
    )
    monkeypatch.setattr(predictions_import, "lock_tasks_for_evidence", lock)
    monkeypatch.setattr(
        predictions_import, "assert_raster_mask_write_enabled", lambda _: None
    )
    monkeypatch.setattr(
        predictions_import, "validate_mask_geometry_for_task", AsyncMock()
    )
    monkeypatch.setattr(predictions_import, "store_mask_reference_objects", store)
    monkeypatch.setattr(predictions_import, "store_coco_rle", store)
    monkeypatch.setattr(
        predictions_import.PredictionService, "create_from_ml_result", create
    )
    importer = (
        predictions_import.import_aap_json
        if format == "aap"
        else predictions_import.import_coco
    )

    if busy:
        with pytest.raises(HTTPException) as caught:
            await importer(db, project_id, _prediction_payload(format))
        assert caught.value.status_code == 409
        assert events == ["task"]
    else:
        result = await importer(db, project_id, _prediction_payload(format))
        assert result.imported == 1 and result.skipped == 0
        assert events == ["task", "store", "prediction"]


async def test_conversion_passes_original_session_to_generated_content(monkeypatch):
    db = object()
    service = AnnotationConversionService(db)
    reference = service._plan_rle_reference(RLE)
    store = AsyncMock(return_value=reference)
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)

    await service._store_generated_rles({reference["object_key"]})

    store.assert_awaited_once_with(RLE, db=db)


@pytest.mark.parametrize("insert_failed", [False, True])
async def test_qc_region_settles_guard_only_after_issue_upsert(
    monkeypatch, insert_failed
):
    reference = build_rle_reference(RLE)
    events = []
    db = SimpleNamespace(commit=AsyncMock())

    caller_db = db

    async def store(rle, *, db):
        assert db is caller_db
        events.append("store")
        return reference

    async def execute(statement):
        events.append("issue")
        if insert_failed:
            raise RuntimeError("issue upsert failed")

    async def bind(caller, value):
        assert caller is db and value == reference
        events.append("bind")

    db.execute = AsyncMock(side_effect=execute)
    module = ModuleType("app.services.raster_mask_objects")
    module.bind_publications = AsyncMock(side_effect=bind)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(mask_qc, "store_coco_rle", store)
    primary = mask_qc._FrameMask(
        annotation_id=uuid.uuid4(),
        annotation_version=1,
        task_id=uuid.uuid4(),
        class_name="object",
        frame_index=None,
        state="manual",
        source=None,
        rle=RLE,
        geometry_digest="a" * 64,
        content_digest="b" * 64,
    )
    kwargs = dict(
        run=SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4()),
        primary=primary,
        related=(),
        code="small_island",
        severity="warning",
        metric={},
        threshold={},
        frame_start=None,
        frame_end=None,
        region_rle=RLE,
        source={},
        seen_keys=set(),
    )

    if insert_failed:
        with pytest.raises(RuntimeError, match="issue upsert failed"):
            await mask_qc._persist_issue(db, **kwargs)
        assert events == ["store", "issue"]
        module.bind_publications.assert_not_awaited()
    else:
        await mask_qc._persist_issue(db, **kwargs)
        assert events == ["store", "issue", "bind"]
    db.commit.assert_not_awaited()
