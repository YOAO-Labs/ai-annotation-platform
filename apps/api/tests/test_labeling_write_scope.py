from __future__ import annotations

from dataclasses import asdict
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from aap_assets_inputs.contract import InputFailure

from app.config import settings
from app.db.models.prediction import Prediction
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from app.services import async_job as async_job_service
from app.services.annotation import AnnotationService
from app.services.batch import BatchService
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultRequest,
)
from app.services.labeling_write_scope import (
    LabelingBatchWriteScope,
    LabelingWriteScope,
    capture_labeling_write_scope,
)
from app.services import labeling_write_guard
from app.services.prediction import PredictionService
from app.services.secondary_inference_result import SecondaryInferenceResult
from app.services.task_lock import TaskLockService
from tests.factory import create_batch, create_membership, create_project, create_task


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["input_busy", "source_busy", "reference_busy"])
async def test_input_source_contention_has_one_retryable_write_code(monkeypatch, code):
    async def raise_busy(*_args, **_kwargs):
        raise InputFailure(code)

    monkeypatch.setattr("app.services.accepted_inputs.lock_input_source", raise_busy)
    scope = type(
        "WriteScope",
        (),
        {
            "accepted_input_id": uuid.uuid4(),
            "task_id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
        },
    )()

    with pytest.raises(HTTPException) as exc_info:
        await labeling_write_guard._lock_accepted_input_source(None, scope)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"reason": "input_busy"}


@pytest.mark.asyncio
async def test_changed_input_source_is_not_mapped_to_contention(monkeypatch):
    async def raise_changed(*_args, **_kwargs):
        raise InputFailure("source_changed")

    monkeypatch.setattr("app.services.accepted_inputs.lock_input_source", raise_changed)
    scope = type(
        "WriteScope",
        (),
        {
            "accepted_input_id": uuid.uuid4(),
            "task_id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
        },
    )()

    with pytest.raises(HTTPException) as exc_info:
        await labeling_write_guard._lock_accepted_input_source(None, scope)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"reason": "prediction_source_changed"}


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
@pytest.mark.parametrize(
    "change", ["path", "file_type", "relink", "path_aba", "generation"]
)
async def test_late_prediction_rejects_changed_task_source_without_task_version_change(
    db_session, project_admin, change
):
    actor, _ = project_admin
    project, task, scope = await _scope_fixture(db_session, actor)
    original_path = task.file_path
    changes = {
        "path": {"file_path": "changed/image.jpg"},
        "file_type": {"file_type": "video"},
        "generation": {"media_source_generation": scope.task_source_generation + 1},
        "path_aba": {"file_path": "changed/image.jpg"},
    }
    if change == "relink":
        dataset = Dataset(
            display_id=f"D-{uuid.uuid4().hex[:8]}",
            name="replacement",
            created_by=actor.id,
        )
        db_session.add(dataset)
        await db_session.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_path=original_path,
            file_name=task.file_name,
            file_type=task.file_type,
        )
        db_session.add(item)
        await db_session.flush()
        values = {"dataset_item_id": item.id}
    else:
        values = changes[change]
    await db_session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if change == "path_aba":
        await db_session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(file_path=original_path)
            .execution_options(synchronize_session=False)
        )
    # Keep the inference caller's stale identity-map object until the final owner lock.
    assert task.version == scope.task_version
    assert task.media_source_generation == scope.task_source_generation

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
    assert failure.value.code == "prediction_source_changed"
    assert not failure.value.retryable
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == task.id)
        )
        == 0
    )
    await db_session.refresh(task)
    assert task.version == scope.task_version
    assert task.media_source_generation > scope.task_source_generation
    assert task.total_predictions == 0
    if change == "path_aba":
        assert task.file_path == original_path


@pytest.mark.asyncio
async def test_legacy_serialized_write_scope_cannot_infer_current_source(
    db_session, project_admin
):
    actor, _ = project_admin
    project, task, scope = await _scope_fixture(db_session, actor)
    payload = asdict(scope)
    for field in (
        "source_binding_known",
        "task_source_generation",
        "source_dataset_item_id",
        "source_file_path",
        "source_file_type",
    ):
        payload.pop(field)
    legacy = LabelingWriteScope(**payload)
    with pytest.raises(ApplyPredictionResultError) as failure:
        await PredictionService(db_session).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project.id,
                ml_backend_id=None,
                result=[],
                write_scope=legacy,
            )
        )
    assert failure.value.code == "labeling_write_scope_unknown"
    assert not failure.value.retryable
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == task.id)
        )
        == 0
    )


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
        await service._apply_secondary_inference(
            task=task, annotation=annotation, result=result, write_scope=delayed_scope
        )
    assert failure.value.status_code == 409
    await db_session.refresh(annotation)
    assert annotation.geometry == new_geometry and "color" not in annotation.attributes

    previous_version = annotation.version
    updated, _ = await service._apply_secondary_inference(
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
