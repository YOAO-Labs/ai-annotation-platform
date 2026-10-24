"""PostgreSQL checks for API AsyncJob reads through the Jobs owner capability."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import os
import re

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.application.async_job_contracts import AsyncJobOperationFailure
from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.application.job_cancellation import SqlJobCancellation
from app.application.prediction_retry import SqlPredictionRetry
from app.db.models.notification import Notification
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from tests.factory import create_project


@asynccontextmanager
async def _without_private_job_select(db_session):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned API role and disposable database")
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    target = db_session.bind.engine.url
    assert (url.host, url.port, url.database) == (
        target.host,
        target.port,
        target.database,
    )
    assert url.database.endswith("_test")
    assert url.username == "identity_api_consumer"
    # The test's outer transaction rolls back this ACL change with its seed rows.
    await db_session.execute(
        text("REVOKE SELECT ON public.async_jobs FROM identity_api_consumer")
    )
    await db_session.execute(text("SET LOCAL ROLE identity_api_consumer"))
    try:
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_any_column_privilege("
                "current_user, 'public.async_jobs', 'SELECT')"
            )
        )
        with pytest.raises(DBAPIError) as denied:
            async with db_session.begin_nested():
                await db_session.execute(text("SELECT id FROM public.async_jobs"))
        assert denied.value.orig.sqlstate == "42501"
        yield
    finally:
        await db_session.execute(text("RESET ROLE"))


@contextmanager
def _sql_capture(db_session):
    statements: list[str] = []
    engine = db_session.bind.sync_engine

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _assert_jobs_owner_read(statements: list[str]) -> None:
    normalized = [statement.lower() for statement in statements]
    assert any("aap_jobs_v1.read_job(" in statement for statement in normalized)
    assert not any(
        re.search(r"\bfrom\s+(?:public\.)?async_jobs\b", statement)
        for statement in normalized
    )


class _UnusedExecutionControl:
    def request_cancel(self, _execution_id: str) -> None:
        raise AssertionError("predictions_import cancellation does not revoke Celery")


class _UnusedRetryDispatcher:
    def dispatch_prediction_retry(self, *_args, **_kwargs) -> None:
        raise AssertionError("denied job retry must not dispatch")

    def dispatch_job_item_retry(self, *_args, **_kwargs) -> None:
        raise AssertionError("denied job retry must not dispatch")


@pytest.mark.asyncio
async def test_detail_authorization_reads_job_through_jobs_owner(db_session, annotator):
    actor, _token = annotator
    job = await async_job_svc.create_job(
        db_session,
        kind="dataset_import",
        user_id=actor.id,
        payload={"dataset_display_id": "DATASET-READ"},
    )
    await async_job_svc.mark_complete(db_session, job.id, result={"imported": 3})
    await db_session.flush()
    job_id = job.id
    db_session.expunge(job)

    async with _without_private_job_select(db_session):
        with _sql_capture(db_session) as statements:
            result = await SqlAsyncJobReadAdapter(db_session).get_job(
                job_id=job_id, actor_id=actor.id
            )

    assert result is not None
    assert result.id == job_id
    assert result.kind == "dataset_import"
    assert result.result == {"imported": 3}
    _assert_jobs_owner_read(statements)


@pytest.mark.asyncio
async def test_cancel_reads_job_through_jobs_owner_and_keeps_terminal_notice(
    db_session, project_admin
):
    actor, _token = project_admin
    project = await create_project(db_session, owner_id=actor.id)
    job = await async_job_svc.create_job(
        db_session,
        kind="predictions_import",
        user_id=actor.id,
        project_id=project.id,
    )
    await db_session.flush()
    job_id = job.id
    db_session.expunge(job)

    async with _without_private_job_select(db_session):
        with _sql_capture(db_session) as statements:
            outcome = await SqlJobCancellation(
                db_session, execution_control=_UnusedExecutionControl()
            ).cancel(job_id=job_id, actor_id=actor.id)
            await db_session.flush()

    assert outcome == "cancelled"
    _assert_jobs_owner_read(statements)
    fresh = await async_job_svc.get_job(db_session, job_id, populate_existing=True)
    assert fresh is not None and fresh.status == "cancelled"
    notice = await db_session.scalar(
        select(Notification).where(
            Notification.user_id == actor.id,
            Notification.target_id == job_id,
        )
    )
    assert notice is not None and notice.type == "job.cancelled"


@pytest.mark.asyncio
async def test_retry_scope_denial_reads_job_through_jobs_owner(
    db_session, project_admin, annotator
):
    owner, _owner_token = project_admin
    actor, _actor_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    job = await async_job_svc.create_job(
        db_session,
        kind="batch_predict",
        user_id=owner.id,
        project_id=project.id,
    )
    job_id = job.id
    db_session.expunge(job)

    async with _without_private_job_select(db_session):
        with _sql_capture(db_session) as statements:
            with pytest.raises(AsyncJobOperationFailure) as denied:
                await SqlPredictionRetry(
                    db_session, dispatcher=_UnusedRetryDispatcher()
                ).retry_job_items(job_id=job_id, actor_id=actor.id)

    assert denied.value.status_code == 403
    assert denied.value.detail == "not your job"
    _assert_jobs_owner_read(statements)


@pytest.mark.asyncio
async def test_terminal_notification_reads_job_through_jobs_owner(
    db_session, annotator
):
    actor, _token = annotator
    job = await async_job_svc.create_job(
        db_session,
        kind="point_cloud_cross_frame",
        user_id=actor.id,
        payload={"scene_name": "read-job scene"},
    )
    await async_job_svc.mark_complete(
        db_session, job.id, result={"created_annotation_count": 1}
    )
    await db_session.flush()
    job_id = job.id
    db_session.expunge(job)

    async with _without_private_job_select(db_session):
        with _sql_capture(db_session) as statements:
            await notify_job_terminal(db_session, job_id=job_id)
            await db_session.flush()

    _assert_jobs_owner_read(statements)
    notice = await db_session.scalar(
        select(Notification).where(
            Notification.user_id == actor.id,
            Notification.target_id == job_id,
        )
    )
    assert notice is not None
    assert notice.type == "job.completed"
    assert notice.payload["created_annotation_count"] == 1
