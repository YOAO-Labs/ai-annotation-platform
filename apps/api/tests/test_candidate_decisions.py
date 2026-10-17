from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.application.candidate_decision import bind_candidate_decision_port
from app.db.models.prediction import Prediction
from app.db.models.task_lock import TaskLock
from app.services.annotation import AnnotationService
from app.services.candidate_decision_contract import (
    CandidateAcceptCommand,
    CandidateRejectCommand,
)
from app.services.labeling_write_scope import capture_labeling_write_scope
from tests.factory import create_project, create_task

pytestmark = pytest.mark.asyncio


def _scope(task, actor_id, operation: str):
    return capture_labeling_write_scope(
        task,
        actor_id=actor_id,
        actor_known=True,
        operation=operation,
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )


async def _seed_candidates(db_session, actor):
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="candidate-decision-test",
        score=0.9,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": index * 10,
                    "y": 0,
                    "width": 5,
                    "height": 5,
                    "rectanglelabels": ["car"],
                },
                "score": 0.9,
            }
            for index in range(3)
        ],
    )
    db_session.add(prediction)
    await db_session.flush()
    return project, task, prediction


async def test_candidate_commands_reject_mismatched_binding_before_sql(
    db_session, super_admin
):
    actor, _token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    await db_session.flush()
    decisions = bind_candidate_decision_port(db_session, task=task, actor=actor)
    prediction_id = uuid.uuid4()
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError):
            await decisions.accept(
                CandidateAcceptCommand(
                    write_scope=replace(
                        _scope(task, actor.id, "candidate_accept"),
                        task_id=uuid.uuid4(),
                    ),
                    prediction_id=prediction_id,
                    shape_index=None,
                    override_class_name=None,
                    attribute_overrides=None,
                    video_segment_id=None,
                )
            )
        with pytest.raises(ValueError):
            await decisions.reject(
                CandidateRejectCommand(
                    write_scope=replace(
                        _scope(task, actor.id, "candidate_reject"),
                        actor_id=uuid.uuid4(),
                    ),
                    prediction_id=prediction_id,
                    shape_index=0,
                )
            )
        assert not statements
        assert task.total_annotations == 0
        assert not db_session.new
        assert not db_session.dirty
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


async def test_manual_candidate_rejection_commits_single_all_and_repeat_indexes(
    db_session, httpx_client, super_admin
):
    actor, token = super_admin
    _project, task, prediction = await _seed_candidates(db_session, actor)
    now = datetime.now(timezone.utc)
    task_lock = TaskLock(
        task_id=task.id,
        user_id=actor.id,
        expire_at=now + timedelta(seconds=30),
    )
    db_session.add(task_lock)
    await db_session.flush()
    original_expiry = task_lock.expire_at
    headers = {"Authorization": f"Bearer {token}"}

    wrong_task = await create_task(
        db_session, project_id=task.project_id, status="in_progress"
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{wrong_task.id}/predictions/{prediction.id}/reject",
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/reject?shape_index=3",
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "shape_index 3 out of range (0..2)"

    single = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/reject?shape_index=1",
        headers=headers,
    )
    assert single.status_code == 204
    await db_session.refresh(task_lock)
    assert task_lock.expire_at > original_expiry

    repeated = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/reject?shape_index=1",
        headers=headers,
    )
    assert repeated.status_code == 204
    pending = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions", headers=headers
    )
    assert pending.status_code == 200
    assert [shape["shape_index"] for shape in pending.json()[0]["result"]] == [0, 2]

    all_shapes = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/reject",
        headers=headers,
    )
    assert all_shapes.status_code == 204
    await db_session.refresh(prediction)
    assert prediction.rejected_shape_indexes == [1, 0, 2]
    complete = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions", headers=headers
    )
    assert complete.status_code == 200
    assert complete.json() == []
    await db_session.refresh(task)
    assert task.total_annotations == 0


async def test_candidate_rejection_restores_projected_http_error_facts(
    db_session, httpx_client, super_admin, monkeypatch
):
    actor, token = super_admin
    _project, task, prediction = await _seed_candidates(db_session, actor)

    async def reject_with_expected_http_error(
        _service,
        _prediction_id,
        *,
        shape_index,
        write_scope,
    ):
        raise HTTPException(
            status_code=409,
            detail={"reason": "candidate_busy", "shape_index": shape_index},
            headers={"X-Candidate-Decision": "busy"},
        )

    monkeypatch.setattr(
        AnnotationService,
        "_reject_prediction",
        reject_with_expected_http_error,
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/reject?shape_index=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "reason": "candidate_busy",
        "shape_index": 0,
    }
    assert response.headers["X-Candidate-Decision"] == "busy"
