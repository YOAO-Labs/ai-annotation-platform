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
from app.db.models.project import Project
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
        "import_aap_json_annotations" if route == "annotations" else "import_aap_json",
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
