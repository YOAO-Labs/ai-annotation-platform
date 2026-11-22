"""Qualify mask publication against the current transactional owners."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.api.v1._mask_content import mask_content_response
from app.application.raster_mask_retention import referenced_raster_mask_keys
from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.mask_qc import MaskQCRunRequest
from app.services import predictions_import
from app.services.labeling_prediction_contract import ApplyPredictionResultError
from app.services.mask_qc import execution as qc_execution
from app.services.mask_qc.service import create_mask_qc_run
from app.services.prediction import PredictionService, to_internal_shape
from app.services.raster_mask_storage import build_rle_reference, load_coco_rle
from tests.factory import create_user
from tests.raster_mask_support import consumer_mask_storage as consumer_mask_storage
from tests.raster_mask_support import harness as harness
from tests.raster_mask_support import _image_project_task, _seed_mask


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


def _prediction_payload(format, rle):
    if format == "aap":
        reference = build_rle_reference(rle)
        return json.dumps(
            {
                "schema_version": "1.3",
                "mask_objects": {reference["sha256"]: rle},
                "tasks": [
                    {
                        "task_match": {"file_path": "image.png"},
                        "media_type": "image",
                        "predictions": [
                            {
                                "class_name": "car",
                                "geometry": {"type": "raster_mask", "mask": reference},
                            }
                        ],
                    }
                ],
            }
        ).encode()
    height, width = rle["size"]
    return json.dumps(
        {
            "images": [
                {"id": 1, "file_name": "image.png", "width": width, "height": height}
            ],
            "categories": [{"id": 1, "name": "car"}],
            "annotations": [{"image_id": 1, "category_id": 1, "segmentation": rle}],
        }
    ).encode()


def _importer(format):
    return (
        predictions_import.import_native_json
        if format == "aap"
        else predictions_import.import_coco
    )


@pytest.mark.parametrize("format", ["aap", "coco"])
@pytest.mark.parametrize("authority", ["current", "missing", "stale"])
async def test_prediction_import_publishes_only_with_current_authority(
    db_session,
    super_admin,
    harness,
    consumer_mask_storage,
    monkeypatch,
    format,
    authority,
):
    user, _ = super_admin
    rle, reference, _ = harness.content()
    height, width = rle["size"]
    project, task = await _image_project_task(
        db_session, owner_id=user.id, file_path="image.png", width=width, height=height
    )
    previous = Prediction(
        task_id=task.id, project_id=project.id, result=[], source="external_import"
    )
    db_session.add(previous)
    task.total_predictions = 1
    await db_session.flush()
    previous_id, task_id, project_id = previous.id, task.id, project.id
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)

    if authority == "stale":
        apply_result = PredictionService.apply_prediction_result

        async def change_source_then_apply(service, request):
            await service.db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(file_path="replacement.png")
                .execution_options(synchronize_session=False)
            )
            return await apply_result(service, request)

        monkeypatch.setattr(
            PredictionService, "apply_prediction_result", change_source_then_apply
        )

    transaction = await db_session.begin_nested()
    if authority == "missing":
        with pytest.raises(HTTPException) as caught:
            await _importer(format)(
                db_session,
                project_id,
                _prediction_payload(format, rle),
                actor_id=None,
                job_id=None,
                overwrite_existing=True,
            )
        assert caught.value.status_code == 401
    elif authority == "stale":
        with pytest.raises(ApplyPredictionResultError) as caught:
            await _importer(format)(
                db_session,
                project_id,
                _prediction_payload(format, rle),
                actor_id=user.id,
                job_id=None,
                overwrite_existing=True,
            )
        assert caught.value.code == "prediction_source_changed"
        assert caught.value.status_code == 409
    else:
        result = await _importer(format)(
            db_session,
            project_id,
            _prediction_payload(format, rle),
            actor_id=user.id,
            job_id=None,
            overwrite_existing=True,
        )
        assert result.imported == 1 and result.skipped == 0 and result.errors == []
        prediction = (
            await db_session.scalars(
                select(Prediction).where(Prediction.task_id == task_id)
            )
        ).one()
        assert prediction.id != previous_id
        assert prediction.source == "external_import"
        assert to_internal_shape(prediction.result[0])["geometry"]["mask"] == reference
        assert reference["object_key"] in await referenced_raster_mask_keys(db_session)

    await transaction.rollback()
    rows = (
        await db_session.scalars(
            select(Prediction).where(Prediction.task_id == task_id)
        )
    ).all()
    assert [row.id for row in rows] == [previous_id]
    await db_session.refresh(task)
    assert task.total_predictions == 1
    assert task.file_path == "image.png"
    assert reference["object_key"] not in await referenced_raster_mask_keys(db_session)
    owners = await harness.rows()
    if authority == "missing":
        assert owners == []
    else:
        assert [(row.logical_key, row.state) for row in owners] == [
            (reference["object_key"], "ready")
        ]


@pytest.mark.parametrize("format", ["aap", "coco"])
async def test_prediction_import_rejects_busy_final_task_fence(
    harness, consumer_mask_storage, monkeypatch, format
):
    rle, reference, _ = harness.content()
    height, width = rle["size"]
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    async with harness.factory() as setup:
        user = await create_user(
            setup, "super_admin", f"{uuid.uuid4().hex}@test.local", "Owner"
        )
        project, task = await _image_project_task(
            setup, owner_id=user.id, file_path="image.png", width=width, height=height
        )
        previous = Prediction(
            task_id=task.id, project_id=project.id, result=[], source="external_import"
        )
        setup.add(previous)
        task.total_predictions = 1
        await setup.flush()
        user_id, project_id, task_id = user.id, project.id, task.id
        item_id = task.dataset_item_id
        dataset_id = (await setup.get(DatasetItem, item_id)).dataset_id
        previous_id = previous.id
        await setup.commit()

    try:
        async with harness.factory() as locker, harness.factory() as writer:
            await locker.scalar(
                select(Task).where(Task.id == task_id).with_for_update()
            )
            with pytest.raises(ApplyPredictionResultError) as caught:
                await _importer(format)(
                    writer,
                    project_id,
                    _prediction_payload(format, rle),
                    actor_id=user_id,
                    job_id=None,
                    overwrite_existing=True,
                )
            assert caught.value.code == "task_busy"
            assert caught.value.status_code == 409
            assert caught.value.retryable
            await writer.rollback()
            await locker.rollback()
        async with harness.factory() as observer:
            rows = (
                await observer.scalars(
                    select(Prediction).where(Prediction.task_id == task_id)
                )
            ).all()
            assert [row.id for row in rows] == [previous_id]
            assert (await observer.get(Task, task_id)).total_predictions == 1
            assert reference["object_key"] not in await referenced_raster_mask_keys(
                observer
            )
            assert [(row.logical_key, row.state) for row in await harness.rows()] == [
                (reference["object_key"], "ready")
            ]
    finally:
        async with harness.factory() as cleanup:
            await cleanup.execute(
                delete(Prediction).where(Prediction.task_id == task_id)
            )
            await cleanup.execute(delete(Task).where(Task.id == task_id))
            await cleanup.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
            await cleanup.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


@pytest.mark.parametrize("insert_failed", [False, True])
async def test_qc_region_and_issue_follow_caller_transaction(
    db_session, super_admin, harness, consumer_mask_storage, insert_failed
):
    user, _ = super_admin
    project, task, annotation, _ = await _seed_mask(db_session, owner_id=user.id)
    run, _, _ = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    project_id = project.id
    rle, reference, _ = harness.content()
    primary = qc_execution._FrameMask(
        annotation_id=annotation.id,
        annotation_version=annotation.version,
        task_id=task.id,
        class_name="object",
        frame_index=None,
        state="manual",
        source=None,
        rle=rle,
        geometry_digest="a" * 64,
        content_digest="b" * 64,
    )
    kwargs = dict(
        run=(
            SimpleNamespace(id=uuid.uuid4(), project_id=project_id)
            if insert_failed
            else run
        ),
        primary=primary,
        related=(),
        code="small_island",
        severity="warning",
        metric={},
        threshold={},
        frame_start=None,
        frame_end=None,
        region_rle=rle,
        source={},
        seen_keys=set(),
    )
    transaction = await db_session.begin_nested()
    if insert_failed:
        with pytest.raises(IntegrityError):
            await qc_execution._persist_issue(db_session, **kwargs)
    else:
        await qc_execution._persist_issue(db_session, **kwargs)
        issue = (
            await db_session.scalars(
                select(MaskQCIssue).where(MaskQCIssue.project_id == project_id)
            )
        ).one()
        assert issue.region_mask_ref == reference
        assert issue.region_digest == reference["sha256"]
        assert reference["object_key"] in await referenced_raster_mask_keys(db_session)

    await transaction.rollback()
    assert (
        await db_session.scalars(
            select(MaskQCIssue).where(MaskQCIssue.project_id == project_id)
        )
    ).all() == []
    assert reference["object_key"] not in await referenced_raster_mask_keys(db_session)
    assert await load_coco_rle(reference) == rle
    assert [(row.logical_key, row.state) for row in await harness.rows()] == [
        (reference["object_key"], "ready")
    ]
