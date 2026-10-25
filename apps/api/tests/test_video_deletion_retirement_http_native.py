"""Real HTTP deletion gates for unretired canonical video executions."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import re
import uuid

import pytest
from sqlalchemy import text

from app.core.security import create_access_token
from tests.test_labeling_counter_deltas_workflows_native import (
    _configure_actual_api,
    _restricted_api_client,
)
from tests.test_project_owner_persistence_native import _restricted
from tests.test_video_deletion_retirement_native import _retire, _seed_unretired

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and actual API LOGIN",
    ),
]

_BLOCKED_DETAIL = {
    "code": "video_execution_not_retired",
    "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
}
_DIRECT_PROJECT_SQL = re.compile(
    r"\b(?:from|join|update|insert\s+into|delete\s+from)\s+"
    r"(?:public\.)?projects\b",
    re.IGNORECASE,
)


@asynccontextmanager
async def _restricted_http_world(test_engine, app_module, monkeypatch):
    _configure_actual_api(test_engine, monkeypatch)
    from app.deps import get_video_tracker_job_dispatcher

    class Dispatcher:
        def __init__(self):
            self.jobs = []

        def dispatch_tracker_job(self, job_id):
            self.jobs.append(job_id)
            return str(uuid.uuid4())

    dispatcher = Dispatcher()
    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_video_tracker_job_dispatcher,
        lambda: dispatcher,
    )
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        from app.middleware import audit as audit_middleware

        monkeypatch.setattr(audit_middleware, "async_session", api)
        statements = []
        # Video journals and business audits are retained until owned DB teardown.
        async with _restricted_api_client(app_module, api, statements) as client:
            yield owner, world, client, statements, dispatcher


async def _seed_fixture(owner, **options):
    async with owner.begin() as db:
        return await _seed_unretired(db, **options)


def _headers(fixture, *, operation_id: uuid.UUID | None = None) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(fixture["actor_id"]), role="super_admin")
    }
    if operation_id is not None:
        headers["Idempotency-Key"] = str(operation_id)
    return headers


async def _request_delete(client, kind, fixture, *, operation_id=None):
    if kind == "project":
        return await client.delete(
            f"/api/v1/projects/{fixture['project_id']}",
            headers=_headers(fixture),
        )
    return await client.delete(
        f"/api/v1/datasets/{fixture['dataset_id']}/link/{fixture['project_id']}",
        headers=_headers(fixture, operation_id=operation_id),
    )


async def _snapshot(owner, fixture):
    async with owner() as db:
        job = (
            await db.execute(
                text(
                    "SELECT status,cancel_requested_at,started_at,completed_at "
                    "FROM public.video_tracker_jobs WHERE id=:job"
                ),
                {"job": fixture["job_id"]},
            )
        ).one_or_none()
        task = (
            await db.execute(
                text(
                    "SELECT project_id,dataset_item_id,status FROM public.tasks "
                    "WHERE id=:task"
                ),
                {"task": fixture["task_id"]},
            )
        ).one_or_none()
        execution = (
            await db.execute(
                text(
                    "SELECT run_id,worker_id,generation,lease_expires_at,state,route_request_id "
                    "FROM aap_jobs_v1.video_executions WHERE job_id=:job"
                ),
                {"job": fixture["job_id"]},
            )
        ).one_or_none()
        journal = (
            await db.execute(
                text(
                    "SELECT writer_terminal,writer_terminal_outcome,execution_retired "
                    "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                ),
                {"job": fixture["job_id"]},
            )
        ).one_or_none()
        return {
            "job": job,
            "task": task,
            "execution": execution,
            "journal": journal,
            "project_exists": await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM public.projects WHERE id=:project)"),
                {"project": fixture["project_id"]},
            ),
            "association_exists": await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM public.project_datasets "
                    "WHERE project_id=:project AND dataset_id=:dataset)"
                ),
                {
                    "project": fixture["project_id"],
                    "dataset": fixture["dataset_id"],
                },
            ),
            "project_receipts": await db.scalar(
                text(
                    "SELECT count(*) FROM public.project_deletion_operations "
                    "WHERE actor_id=:actor AND project_id=:project"
                ),
                {
                    "actor": fixture["actor_id"],
                    "project": fixture["project_id"],
                },
            ),
            "unlink_receipts": await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_unlink_operations "
                    "WHERE actor_id=:actor AND dataset_id=:dataset AND project_id=:project"
                ),
                {
                    "actor": fixture["actor_id"],
                    "dataset": fixture["dataset_id"],
                    "project": fixture["project_id"],
                },
            ),
            "asset_outbox": await db.scalar(
                text("SELECT count(*) FROM public.asset_storage_deletions")
            ),
            "business_deletion_audits": await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor AND "
                    "((action='project.delete' AND target_type='project' AND target_id=:project) "
                    "OR (action='dataset.unlink' AND target_type='dataset' AND target_id=:dataset))"
                ),
                {
                    "actor": fixture["actor_id"],
                    "project": str(fixture["project_id"]),
                    "dataset": str(fixture["dataset_id"]),
                },
            ),
        }


async def _assert_retired_lease_stays_live(owner, fixture) -> None:
    async with owner() as db:
        row = (
            await db.execute(
                text(
                    "SELECT journal.execution_retired, "
                    "execution.lease_expires_at > pg_catalog.clock_timestamp() "
                    "FROM aap_jobs_v1.video_execution_journals AS journal "
                    "JOIN aap_jobs_v1.video_executions AS execution "
                    "ON execution.job_id=journal.job_id WHERE journal.job_id=:job"
                ),
                {"job": fixture["job_id"]},
            )
        ).one()
    assert row == (True, True)


def _assert_blocked(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": _BLOCKED_DETAIL}


def _assert_success(kind, response, operation_id):
    if kind == "project":
        assert response.status_code == 204, response.text
        assert response.content == b""
        return
    assert response.status_code == 200, response.text
    assert response.headers["Idempotency-Key"] == str(operation_id)
    assert response.json() == {
        "deleted_tasks": 1,
        "deleted_annotations": 0,
        "deleted_batches": 0,
        "deleted_batch_ids": [],
        "soft": False,
    }


def _assert_no_direct_project_sql(statements) -> None:
    assert not [sql for sql in statements if _DIRECT_PROJECT_SQL.search(sql)]


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_cancelled_unretired_execution_blocks_and_retries_after_retirement(
    test_engine, app_module, monkeypatch, kind
):
    async with _restricted_http_world(test_engine, app_module, monkeypatch) as (
        owner,
        _world,
        client,
        statements,
        dispatcher,
    ):
        fixture = await _seed_fixture(owner, bound=False, status="running")
        operation_id = uuid.uuid4()
        before = await _snapshot(owner, fixture)
        assert before["job"][0] == "running"
        assert before["journal"][2] is False
        _assert_blocked(
            await _request_delete(client, kind, fixture, operation_id=operation_id)
        )
        assert await _snapshot(owner, fixture) == before

        cancelled = await client.delete(
            f"/api/v1/video-tracker-jobs/{fixture['job_id']}",
            headers=_headers(fixture),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert dispatcher.jobs == [fixture["job_id"]]
        after_cancel = await _snapshot(owner, fixture)
        assert after_cancel["journal"][2] is False
        _assert_blocked(
            await _request_delete(client, kind, fixture, operation_id=operation_id)
        )
        assert await _snapshot(owner, fixture) == after_cancel

        async with owner.begin() as db:
            await _retire(db, fixture)
        await _assert_retired_lease_stays_live(owner, fixture)
        response = await _request_delete(
            client, kind, fixture, operation_id=operation_id
        )
        _assert_success(kind, response, operation_id)
        after = await _snapshot(owner, fixture)
        if kind == "project":
            assert not after["project_exists"]
            assert after["project_receipts"] == before["project_receipts"] + 1
        else:
            assert after["project_exists"] and not after["association_exists"]
            assert after["unlink_receipts"] == before["unlink_receipts"] + 1
        assert (
            after["business_deletion_audits"] == before["business_deletion_audits"] + 1
        )
        _assert_no_direct_project_sql(statements)


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_terminal_job_with_unretired_current_journal_still_blocks_http_deletion(
    test_engine, app_module, monkeypatch, kind, status
):
    async with _restricted_http_world(test_engine, app_module, monkeypatch) as (
        owner,
        _world,
        client,
        statements,
        dispatcher,
    ):
        fixture = await _seed_fixture(
            owner, bound=True, journal=True, status=status, claimed=True
        )
        operation_id = uuid.uuid4()
        before = await _snapshot(owner, fixture)
        assert before["job"][0] == status
        assert before["journal"][2] is False
        _assert_blocked(
            await _request_delete(client, kind, fixture, operation_id=operation_id)
        )
        assert await _snapshot(owner, fixture) == before
        _assert_no_direct_project_sql(statements)
        assert dispatcher.jobs == []


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_unbound_execution_can_be_deleted_after_native_retirement_with_live_lease(
    test_engine, app_module, monkeypatch, kind
):
    async with _restricted_http_world(test_engine, app_module, monkeypatch) as (
        owner,
        _world,
        client,
        statements,
        dispatcher,
    ):
        fixture = await _seed_fixture(
            owner, bound=False, journal=True, status="cancelled", claimed=True
        )
        operation_id = uuid.uuid4()
        async with owner.begin() as db:
            await _retire(db, fixture)
        await _assert_retired_lease_stays_live(owner, fixture)
        before = await _snapshot(owner, fixture)
        assert before["journal"][2] is True
        assert dispatcher.jobs == []
        response = await _request_delete(
            client, kind, fixture, operation_id=operation_id
        )
        _assert_success(kind, response, operation_id)
        after = await _snapshot(owner, fixture)
        if kind == "project":
            assert not after["project_exists"]
            assert after["project_receipts"] == before["project_receipts"] + 1
        else:
            assert after["project_exists"] and not after["association_exists"]
            assert after["unlink_receipts"] == before["unlink_receipts"] + 1
        assert (
            after["business_deletion_audits"] == before["business_deletion_audits"] + 1
        )
        _assert_no_direct_project_sql(statements)
