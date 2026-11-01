"""A storage failure cannot commit a purge while recording import failure."""

from io import BytesIO
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException, UploadFile
import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.api.v1 import annotations, predictions
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.schemas.native_json import ImportResult
from app.services import annotations_import, predictions_import
from app.services.raster_mask_storage import RasterMaskContractError
from tests.test_image_raster_mask_api import _seed_image_mask


@pytest.mark.parametrize("route", ["annotations", "predictions"])
async def test_failed_storage_rolls_back_real_import_before_failure_receipt(
    db_session, super_admin, monkeypatch, route
):
    user, _ = super_admin
    task, original = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    project = await db_session.get(Project, task.project_id)
    original_id, task_id, project_id, user_id = (
        original.id,
        task.id,
        project.id,
        user.id,
    )
    inserted_id = uuid.uuid4()
    await db_session.commit()

    async def fail_after_partial_write(db, *args, **kwargs):
        await db.execute(delete(Annotation).where(Annotation.id == original_id))
        db.add(
            Annotation(
                id=inserted_id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
                annotation_type="bbox",
                class_name="object",
                geometry={"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
            )
        )
        await db.flush()
        raise RasterMaskContractError(
            status_code=503,
            reason="raster_object_storage_unavailable",
            message="mask storage is unavailable",
            retryable=True,
        )

    importer = AsyncMock(side_effect=fail_after_partial_write)
    monkeypatch.setattr(
        annotations_import if route == "annotations" else predictions_import,
        "import_native_json_annotations"
        if route == "annotations"
        else "import_native_json",
        importer,
    )
    upload = UploadFile(BytesIO(b"{}"), filename="probe.json")
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as caught:
        if route == "annotations":
            await annotations.import_annotations(
                request=request,
                file=upload,
                format="aap_json",
                dry_run=False,
                overwrite=True,
                project=project,
                db=db_session,
                current_user=user,
            )
        else:
            await predictions.import_predictions(
                request=request,
                files=[upload],
                format="aap_json",
                yolo_variant="det",
                dry_run=False,
                model_version=None,
                overwrite_existing=True,
                image_width=None,
                image_height=None,
                project=project,
                db=db_session,
                current_user=user,
            )
    assert caught.value.status_code == 503
    assert caught.value.detail["reason"] == "raster_object_storage_unavailable"
    assert await db_session.get(Annotation, original_id) is not None
    assert await db_session.get(Annotation, inserted_id) is None
    jobs = list(
        await db_session.scalars(
            select(AsyncJob).where(AsyncJob.project_id == project_id)
        )
    )
    assert len(jobs) == 1 and jobs[0].status == "failed"


@pytest.mark.parametrize("format", ["aap_json", "coco"])
@pytest.mark.parametrize("prior_file", [False, True])
@pytest.mark.parametrize("failure_kind", ["mask", "unexpected"])
async def test_prediction_multifile_terminal_failure_rolls_back_all_business_writes(
    db_session, super_admin, monkeypatch, format, prior_file, failure_kind
):
    user, _ = super_admin
    task, _ = await _seed_image_mask(db_session, owner_id=user.id, user_id=user.id)
    project = await db_session.get(Project, task.project_id)
    task_id, project_id = task.id, project.id
    original_id, prior_id, failed_id = (uuid.uuid4() for _ in range(3))
    db_session.add(
        Prediction(
            id=original_id,
            task_id=task_id,
            project_id=project_id,
            result=[],
            source="external_import",
        )
    )
    await db_session.commit()

    error = (
        RasterMaskContractError(
            status_code=503,
            reason="raster_object_storage_unavailable",
            message="mask storage is unavailable",
            retryable=True,
        )
        if failure_kind == "mask"
        else RuntimeError("unexpected import failure")
    )

    async def import_file(db, _project_id, raw, **kwargs):
        if raw == b"later":
            raise AssertionError("file after terminal failure was processed")
        await db.execute(delete(Prediction).where(Prediction.id == original_id))
        db.add(
            Prediction(
                id=prior_id if raw == b"prior" else failed_id,
                task_id=task_id,
                project_id=project_id,
                result=[],
                source="external_import",
            )
        )
        await db.flush()
        if raw == b"fail":
            raise error
        return ImportResult(imported=1)

    monkeypatch.setattr(
        predictions_import,
        "import_native_json" if format == "aap_json" else "import_coco",
        import_file,
    )
    contents = ([b"prior"] if prior_file else []) + [b"fail", b"later"]
    files = [
        UploadFile(BytesIO(raw), filename=f"{raw.decode()}.json") for raw in contents
    ]
    expected = HTTPException if failure_kind == "mask" else RuntimeError
    with pytest.raises(expected) as caught:
        await predictions.import_predictions(
            request=Request({"type": "http", "headers": []}),
            files=files,
            format=format,
            yolo_variant="det",
            dry_run=False,
            model_version=None,
            overwrite_existing=True,
            image_width=None,
            image_height=None,
            project=project,
            db=db_session,
            current_user=user,
        )
    if failure_kind == "mask":
        assert caught.value.status_code == 503
        assert caught.value.detail == error.detail
    else:
        assert caught.value is error

    prediction_ids = set(
        await db_session.scalars(
            select(Prediction.id).where(Prediction.project_id == project_id)
        )
    )
    assert prediction_ids == {original_id}
    jobs = list(
        await db_session.scalars(
            select(AsyncJob).where(AsyncJob.project_id == project_id)
        )
    )
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert jobs[0].error_message == str(error)
