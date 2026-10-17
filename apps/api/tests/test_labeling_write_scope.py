from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.db.models.prediction import Prediction
from app.services import async_job as async_job_service
from app.services.annotation import AnnotationService
from app.services.batch import BatchService
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultRequest,
)
from app.services.labeling_write_scope import (
    LabelingBatchWriteScope,
    capture_labeling_write_scope,
)
from app.services.prediction import PredictionService
from app.services.secondary_inference_result import SecondaryInferenceResult
from app.services.task_lock import TaskLockService
from tests.factory import create_batch, create_membership, create_project, create_task


async def _scope_fixture(db, owner):
    project = await create_project(db, owner_id=owner.id)
    task = await create_task(db, project_id=project.id)
    return (
        project,
        task,
        capture_labeling_write_scope(
            task,
            actor_id=owner.id,
            actor_known=True,
            operation="preannotation",
            execution_scope="bulk",
            execution_scope_known=True,
            job_id=None,
            job_id_known=True,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "expected_status"),
    [
        ("account_disabled", 401),
        ("ownership_transferred", 404),
        ("entered_review", 409),
        ("reassigned", 409),
        ("editor_acquired", 409),
        ("cancelled_execution", 409),
    ],
)
async def test_late_prediction_does_not_land_after_scope_changes(
    db_session, project_admin, super_admin, annotator, change, expected_status
):
    actor, _ = project_admin
    recipient, _ = super_admin
    editor, _ = annotator
    project, task, scope = await _scope_fixture(db_session, actor)
    await create_membership(
        db_session, project_id=project.id, user_id=editor.id, role="annotator"
    )
    if change == "account_disabled":
        actor.is_active = False
    elif change == "ownership_transferred":
        project.owner_id = recipient.id
    elif change == "entered_review":
        task.status = "review"
    elif change == "reassigned":
        task.assignee_id = editor.id
    elif change == "editor_acquired":
        assert await TaskLockService(db_session).acquire(task.id, editor.id)
    else:
        job = await async_job_service.create_job(
            db_session,
            kind="batch_predict",
            user_id=actor.id,
            project_id=project.id,
            payload={"batch_id": None, "execution_scope": "bulk"},
        )
        await async_job_service.mark_running(db_session, job.id)
        scope = capture_labeling_write_scope(
            task,
            actor_id=actor.id,
            actor_known=True,
            operation="preannotation",
            execution_scope="bulk",
            execution_scope_known=True,
            job_id=job.id,
            job_id_known=True,
        )
        await async_job_service.request_cancel(db_session, job.id)
        await async_job_service.mark_cancelled(db_session, job.id)
    await db_session.flush()

    with pytest.raises(ApplyPredictionResultError) as failure:
        await PredictionService(db_session).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project.id,
                ml_backend_id=None,
                result=[],
                write_scope=scope,
            )
        )

    assert failure.value.status_code == expected_status
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == task.id)
        )
        == 0
    )
    await db_session.refresh(task)
    assert task.total_predictions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["editor_acquired", "entered_manual_phase"])
async def test_batch_does_not_advance_over_current_manual_work(
    db_session, project_admin, annotator, change
):
    actor, _ = project_admin
    editor, _ = annotator
    project = await create_project(db_session, owner_id=actor.id)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id)
    task.batch_id = batch.id
    await create_membership(
        db_session, project_id=project.id, user_id=editor.id, role="annotator"
    )
    scope = LabelingBatchWriteScope(
        batch_id=batch.id,
        project_id=project.id,
        actor_id=actor.id,
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    if change == "editor_acquired":
        assert await TaskLockService(db_session).acquire(task.id, editor.id)
    else:
        task.status = "in_progress"
    await db_session.flush()

    assert not await BatchService(db_session).mark_preannotated_after_prediction(scope)
    await db_session.refresh(batch)
    assert batch.status == "active"

    if change == "editor_acquired":
        await TaskLockService(db_session).release(task.id, editor.id)
    else:
        task.status = "pending"
        await db_session.flush()
    assert await BatchService(db_session).mark_preannotated_after_prediction(scope)
    await db_session.refresh(batch)
    assert batch.status == "pre_annotated"


@pytest.mark.asyncio
async def test_secondary_result_rejects_changed_roi_and_versions_actual_ai_write(
    db_session, project_admin, httpx_client
):
    actor, token = project_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    service = AnnotationService(db_session)
    annotation = await service.create(
        task_id=task.id,
        user_id=actor.id,
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    )

    def capture():
        return capture_labeling_write_scope(
            task,
            actor_id=actor.id,
            actor_known=True,
            operation="secondary_inference",
            execution_scope=None,
            execution_scope_known=True,
            job_id=None,
            job_id_known=True,
            source_annotation=annotation,
        )

    delayed_scope = capture()
    result = SecondaryInferenceResult(
        write_target="attributes",
        model_ref=(("model_id", "roi-classifier"),),
        attributes=(("color", "blue"),),
        geometry_shapes=(),
    )
    new_geometry = {"type": "bbox", "x": 0.4, "y": 0.3, "w": 0.2, "h": 0.2}
    await service.update(annotation.id, geometry=new_geometry)

    with pytest.raises(HTTPException) as failure:
        await service.apply_secondary_inference(
            task=task, annotation=annotation, result=result, write_scope=delayed_scope
        )
    assert failure.value.status_code == 409
    await db_session.refresh(annotation)
    assert annotation.geometry == new_geometry and "color" not in annotation.attributes

    previous_version = annotation.version
    updated, _ = await service.apply_secondary_inference(
        task=task, annotation=annotation, result=result, write_scope=capture()
    )
    await db_session.commit()
    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        headers={
            "Authorization": f"Bearer {token}",
            "If-Match": f'W/"{previous_version}"',
        },
        json={"attributes": {"color": "manual"}},
    )
    assert response.status_code == 409, response.text
    await db_session.refresh(updated)
    assert updated.attributes["color"] == "blue"
    assert updated.attributes_meta["color"]["origin"] == "ai"
    assert updated.geometry == new_geometry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "enabled"), [("production", True), ("development", False)]
)
async def test_actorless_fixture_writer_cannot_bypass_environment_gate(
    db_session, super_admin, monkeypatch, environment, enabled
):
    owner, _ = super_admin
    project, task, _ = await _scope_fixture(db_session, owner)
    scope = capture_labeling_write_scope(
        task,
        actor_id=None,
        actor_known=True,
        operation="test_seed",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "e2e_seed_enabled", enabled)
    with pytest.raises(HTTPException) as failure:
        await PredictionService(db_session).create_test_seed_prediction(
            task_id=task.id, project_id=project.id, result=[], write_scope=scope
        )
    assert failure.value.status_code == 403
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == task.id)
        )
        == 0
    )
