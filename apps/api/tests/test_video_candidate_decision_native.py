from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.tasks import router
from app.application.candidate_decision import bind_candidate_decision_port
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.deps import get_db
from app.services.candidate_decision_contract import (
    CandidateDecisionError,
    CandidateRejectCommand,
)
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.labeling_write_scope import capture_labeling_write_scope
from tests.factory import create_task
from tests.test_jobs_creation_runtime_native import _lan_runtime
from tests.test_video_derivative_task_fence import (
    _http_authority,
    _release_observed_wait,
    video_task as video_task,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]


def _polygons():
    return [
        {
            "type": "video_track_polygon",
            "class_name": "car",
            "geometry": {
                "type": "video_track_polygon",
                "track_id": f"candidate-{index}",
                "keyframes": [
                    {
                        "frame_index": index,
                        "points": [[0.2, 0.2], [0.8, 0.2], [0.5, 0.8]],
                    }
                ],
                "outside": [],
            },
        }
        for index in range(2)
    ]


@pytest.fixture
async def candidate_runtime(test_engine, video_task):
    owner, actor, task, member_id, _ = video_task
    engine = await _lan_runtime(test_engine)
    runtime = async_sessionmaker(engine, expire_on_commit=False)
    physical_id, historical_id, other_id = (uuid.uuid4() for _ in range(3))
    other_task_id = None
    try:
        async with owner.begin() as db:
            await db.execute(
                update(Project)
                .where(Project.id == task.project_id)
                .values(data_type="video")
            )
            other = await create_task(
                db, project_id=task.project_id, status="in_progress"
            )
            other_task_id = other.id
            db.add_all(
                Prediction(
                    id=prediction_id,
                    task_id=task_id,
                    project_id=task.project_id,
                    source=source,
                    model_version="native-candidate-test",
                    score=0.9,
                    result=_polygons(),
                )
                for prediction_id, task_id, source in (
                    (physical_id, task.id, "ml_backend"),
                    (historical_id, task.id, "external_import"),
                    (other_id, other.id, "external_import"),
                )
            )
        yield (
            owner,
            runtime,
            actor,
            task,
            member_id,
            physical_id,
            historical_id,
            other_id,
        )
    finally:
        await engine.dispose()
        async with owner.begin() as db:
            await db.execute(delete(Annotation).where(Annotation.task_id == task.id))
            await db.execute(delete(TaskLock).where(TaskLock.task_id == task.id))
            await db.execute(
                delete(Prediction).where(
                    Prediction.id.in_((physical_id, historical_id, other_id))
                )
            )
            if other_task_id is not None:
                await db.execute(delete(Task).where(Task.id == other_task_id))


@asynccontextmanager
async def _client(db, actor):
    async def request_db():
        try:
            yield db
        finally:
            if db.in_transaction():
                await db.rollback()

    api = FastAPI()
    api.include_router(router, prefix="/api/v1")
    api.dependency_overrides[get_db] = request_db
    api.dependency_overrides[get_identity_gateway] = lambda: IdentityGateway()
    token = create_access_token(str(actor.id), actor.role)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


def _url(task, prediction_id, decision="reject"):
    return (
        f"/api/v1/tasks/{task.id}/predictions/{prediction_id}/{decision}?shape_index=1"
    )


async def _assert_persisted(owner, task_id, prediction_id, *, rejected=(), adopted=()):
    async with owner() as db:
        prediction = await db.scalar(
            select(Prediction).where(Prediction.id == prediction_id)
        )
        assert prediction is not None
        assert prediction.result == _polygons()
        assert prediction.rejected_shape_indexes == list(rejected)
        rows = (
            await db.execute(
                select(
                    Annotation.parent_prediction_id,
                    Annotation.attributes["_shape_index"].astext,
                ).where(Annotation.task_id == task_id)
            )
        ).all()
        assert rows == [(prediction_id, str(index)) for index in adopted]


@pytest.mark.parametrize(
    "decision_case", ["historical-reject", "physical-reject", "historical-accept"]
)
async def test_video_candidate_decision_waits_for_same_task_original_reader(
    candidate_runtime, decision_case
):
    owner, runtime, actor, task, _, physical_id, historical_id, _ = candidate_runtime
    candidate, decision = decision_case.split("-")
    prediction_id = physical_id if candidate == "physical" else historical_id
    async with (
        runtime() as reader,
        runtime() as writer,
        _client(writer, actor) as client,
    ):
        assert (
            await reader.scalar(text("SELECT session_user")) == "identity_api_consumer"
        )
        assert (
            await writer.scalar(text("SELECT session_user")) == "identity_api_consumer"
        )
        assert not await writer.scalar(
            text("SELECT has_any_column_privilege('public.predictions', 'UPDATE')")
        )
        authority = await _http_authority(reader, task, actor)
        assert authority.mode == "http" and authority.owner_id == task.id
        response = await _release_observed_wait(
            reader, writer, client.post(_url(task, prediction_id, decision))
        )
        if decision == "accept":
            assert response.status_code == 200, response.text
            assert [row["attributes"]["_shape_index"] for row in response.json()] == [1]
        else:
            assert response.status_code == 204, response.text
            if candidate == "historical":
                refused = await client.post(_url(task, prediction_id, "accept"))
                assert refused.status_code == 409
                assert refused.json()["detail"] == "Prediction shape was rejected"
    await _assert_persisted(
        owner,
        task.id,
        prediction_id,
        rejected=(1,) if decision == "reject" else (),
        adopted=(1,) if decision == "accept" else (),
    )


@pytest.mark.parametrize(
    "precondition",
    [
        "exclusive-editor",
        "active-editor",
        "revoked-membership",
        "changed-source",
        "other-candidate",
    ],
)
async def test_video_candidate_decision_preserves_write_preconditions(
    candidate_runtime, precondition
):
    owner, runtime, actor, task, member_id, physical_id, _, other_id = candidate_runtime
    async with owner() as setup, runtime() as writer, _client(writer, actor) as client:
        if precondition == "changed-source":
            target = await get_core_row(writer, task_row, task.id)
            scope = capture_labeling_write_scope(
                target,
                actor_id=actor.id,
                actor_known=True,
                operation="candidate_reject",
                execution_scope=None,
                execution_scope_known=True,
                job_id=None,
                job_id_known=True,
            )
            await setup.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(file_path=f"replacement/{task.id}.mp4")
            )
            await setup.commit()
            with pytest.raises(CandidateDecisionError) as changed:
                decisions = bind_candidate_decision_port(
                    writer, task=target, actor=actor
                )
                await decisions.reject(
                    CandidateRejectCommand(
                        write_scope=scope, prediction_id=physical_id, shape_index=1
                    )
                )
            assert (changed.value.status_code, changed.value.detail) == (
                409,
                {"reason": "prediction_source_changed"},
            )
        elif precondition == "exclusive-editor":
            await setup.execute(
                update(Task).where(Task.id == task.id).values(total_annotations=1)
            )
            await writer.execute(text("SET LOCAL statement_timeout = '300ms'"))
            response = await client.post(_url(task, physical_id))
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == {
                "code": "prediction_video_acceptance_busy"
            }
        elif precondition == "active-editor":
            editor = await setup.scalar(
                select(Project.owner_id).where(Project.id == task.project_id)
            )
            assert editor != actor.id
            setup.add(
                TaskLock(
                    task_id=task.id,
                    user_id=editor,
                    expire_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
            )
            await setup.commit()
            response = await client.post(_url(task, physical_id))
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == {
                "code": "prediction_acceptance_task_locked"
            }
        elif precondition == "revoked-membership":
            await setup.execute(
                delete(ProjectMember).where(ProjectMember.id == member_id)
            )
            await setup.commit()
            response = await client.post(_url(task, physical_id))
            assert response.status_code == 404, response.text
        else:
            response = await client.post(_url(task, other_id))
            assert response.status_code == 404, response.text
            assert response.json()["detail"] == "Prediction not found"
            async with owner() as db:
                assert (
                    await db.scalar(
                        select(Prediction.rejected_shape_indexes).where(
                            Prediction.id == other_id
                        )
                    )
                    == []
                )
    await _assert_persisted(owner, task.id, physical_id)
