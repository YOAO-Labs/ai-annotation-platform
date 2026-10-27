"""Retained request Sessions recheck elevated job authority after role changes."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    RetryCredentialAuthority,
)
from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.application.prediction_retry import SqlPredictionRetry
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project, create_user


@asynccontextmanager
async def _committed_job_authority_rows(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    actor_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    failed_id = uuid.uuid4()
    project_job_id = uuid.uuid4()
    global_job_id = uuid.uuid4()

    async with sessions() as setup:
        actor = await create_user(
            setup, "super_admin", f"job-authority-{suffix}@test.local", "Job admin"
        )
        owner = await create_user(
            setup, "project_admin", f"job-owner-{suffix}@test.local", "Job owner"
        )
        actor_id, owner_id = actor.id, owner.id
        project = await create_project(setup, owner_id=owner.id, name="Job authority")
        project_id = project.id
        failure = FailedPrediction(
            id=failed_id,
            project_id=project.id,
            task_id=None,
            ml_backend_id=None,
            error_type="TimeoutError",
            message="retry authority fixture",
        )
        project_job = AsyncJob(
            id=project_job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=owner.id,
            status="completed",
            payload={},
            result={},
        )
        global_job = AsyncJob(
            id=global_job_id,
            kind="dataset_import",
            project_id=None,
            user_id=owner.id,
            status="completed",
            payload={},
            result={"failed_prediction_ids": [str(failed_id)]},
        )
        setup.add_all([failure, project_job, global_job])
        await setup.commit()

    try:
        yield (
            sessions,
            actor_id,
            owner_id,
            project_id,
            failed_id,
            project_job_id,
            global_job_id,
        )
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AsyncJob).where(AsyncJob.id.in_([project_job_id, global_job_id]))
            )
            await cleanup.execute(
                delete(FailedPrediction).where(FailedPrediction.id == failed_id)
            )
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id.in_([actor_id, owner_id])))
            await cleanup.commit()


async def _change_actor(sessions, actor_id: uuid.UUID, **values) -> None:
    async with sessions() as change:
        await change.execute(update(User).where(User.id == actor_id).values(**values))
        await change.commit()


class _RecordingRetryDispatcher:
    def __init__(self) -> None:
        self.prediction: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        self.job_items: list[tuple[uuid.UUID, uuid.UUID]] = []

    def dispatch_prediction_retry(
        self, failed_id: uuid.UUID, actor_id: uuid.UUID, *, execution_queue: str
    ) -> None:
        self.prediction.append((failed_id, actor_id, execution_queue))

    def dispatch_job_item_retry(
        self, failed_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        self.job_items.append((failed_id, actor_id))


@pytest.mark.parametrize("change", [{"role": "employee"}, {"is_active": False}])
async def test_async_job_list_refreshes_cached_super_admin_before_scoping(
    test_engine, change
):
    async with _committed_job_authority_rows(test_engine) as rows:
        sessions, actor_id, _, _, _, project_job_id, global_job_id = rows
        async with sessions() as retained:
            actor = await retained.get(User, actor_id)
            assert actor is not None and actor.role == "super_admin"
            await _change_actor(sessions, actor_id, **change)
            assert actor.role == "super_admin" and actor.is_active

            page = await SqlAsyncJobReadAdapter(retained).list_jobs(
                actor_id=actor_id,
                status=None,
                kind=None,
                project_id=None,
                search=None,
                limit=50,
                offset=0,
            )

            assert page.total == 0
            assert {item.id for item in page.items}.isdisjoint(
                {project_job_id, global_job_id}
            )


@pytest.mark.parametrize("change", [{"role": "employee"}, {"is_active": False}])
async def test_async_job_detail_refreshes_cached_super_admin_before_access(
    test_engine, change
):
    async with _committed_job_authority_rows(test_engine) as rows:
        sessions, actor_id, _, _, _, project_job_id, _ = rows
        async with sessions() as retained:
            actor = await retained.get(User, actor_id)
            assert actor is not None and actor.role == "super_admin"
            await _change_actor(sessions, actor_id, **change)
            assert actor.role == "super_admin" and actor.is_active

            with pytest.raises(AsyncJobOperationFailure) as denied:
                await SqlAsyncJobReadAdapter(retained).get_job(
                    job_id=project_job_id, actor_id=actor_id
                )

            assert denied.value.status_code == 403
            assert denied.value.detail == "not your job"


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [({"role": "employee"}, 403), ({"is_active": False}, 403)],
)
async def test_global_job_retry_refreshes_cached_super_admin_before_claim(
    test_engine, change, expected_status
):
    async with _committed_job_authority_rows(test_engine) as rows:
        sessions, actor_id, _, _, failed_id, _, global_job_id = rows
        async with sessions() as retained:
            actor = await retained.get(User, actor_id)
            assert actor is not None and actor.role == "super_admin"
            await _change_actor(sessions, actor_id, **change)
            assert actor.role == "super_admin" and actor.is_active
            dispatcher = _RecordingRetryDispatcher()

            with pytest.raises(AsyncJobOperationFailure) as denied:
                await SqlPredictionRetry(
                    retained, dispatcher=dispatcher
                ).retry_job_items(
                    job_id=global_job_id,
                    actor_id=actor_id,
                    credential=RetryCredentialAuthority(
                        actor_id=actor_id, authorization="Bearer stale"
                    ),
                )

            assert denied.value.status_code == expected_status
            assert denied.value.detail == "not your job"
            assert dispatcher.job_items == []
            failed = await retained.get(FailedPrediction, failed_id)
            assert failed is not None
            assert failed.retry_count == 0
            assert not (failed.extra or {}).get("retry_pending")


@pytest.mark.parametrize(
    ("change", "expected_status", "expected_detail", "expected_headers"),
    [
        ({"role": "employee"}, 404, "项目不存在", None),
        (
            {"is_active": False},
            401,
            "账号已停用或不存在",
            {"WWW-Authenticate": "Bearer"},
        ),
    ],
)
async def test_failed_prediction_retry_refreshes_cached_super_admin_before_claim(
    test_engine, change, expected_status, expected_detail, expected_headers
):
    async with _committed_job_authority_rows(test_engine) as rows:
        sessions, actor_id, _, _, failed_id, _, _ = rows
        async with sessions() as retained:
            actor = await retained.get(User, actor_id)
            assert actor is not None and actor.role == "super_admin"
            await _change_actor(sessions, actor_id, **change)
            assert actor.role == "super_admin" and actor.is_active
            dispatcher = _RecordingRetryDispatcher()

            with pytest.raises(AsyncJobOperationFailure) as denied:
                await SqlPredictionRetry(
                    retained, dispatcher=dispatcher
                ).retry_prediction(
                    failed_id=failed_id,
                    actor_id=actor_id,
                    credential=RetryCredentialAuthority(
                        actor_id=actor_id, authorization="Bearer stale"
                    ),
                )

            assert denied.value.status_code == expected_status
            assert denied.value.detail == expected_detail
            assert denied.value.headers == expected_headers
            assert dispatcher.prediction == []
            failed = await retained.get(FailedPrediction, failed_id)
            assert failed is not None
            assert failed.retry_count == 0
            assert not (failed.extra or {}).get("retry_pending")
