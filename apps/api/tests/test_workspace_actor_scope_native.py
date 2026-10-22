"""Actor-scoped consumers through the real API LOGIN without private table reads."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.config import settings
from app.core.security import create_access_token
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.notification import NotificationService
from app.services.project_aggregates import membership_project_ids, owned_project_ids
from tests.factory import create_batch, create_membership, create_project, create_task
from tests.test_workspace_actor_scope_legacy import (
    _ACTORS,
    _OPERATIONS,
    _check_scope_case,
    _seed,
)
from tests.test_workspace_project_authority_locking import _world
from tests.test_workspace_project_exclusive_native import _configure_api_auth
from tests.workspace_project_query_helpers import project_query_briefs_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


@pytest.mark.parametrize("actor_case", _ACTORS)
@pytest.mark.parametrize(
    "operation,roles",
    _OPERATIONS,
    ids=[
        kind + ":" + ("any" if not roles else roles[0]) for kind, roles in _OPERATIONS
    ],
)
async def test_api_login_scopes_match_frozen_results(
    test_engine, actor_case, operation, roles
):
    await _check_scope_case(test_engine, actor_case, operation, roles, binding="api")


async def _assert_private_denied(db):
    assert await db.scalar(text("SELECT current_user")) == "identity_api_consumer"
    for table in ("users", "projects", "project_members"):
        with pytest.raises(DBAPIError) as denied:
            async with db.begin_nested():
                await db.execute(text(f"SELECT id FROM public.{table} LIMIT 1"))
        assert denied.value.orig.sqlstate == "42501"


@asynccontextmanager
async def _consumer_world(test_engine, *, role="reviewer"):
    async with _world(test_engine) as (owner, api, world, url):
        async with owner.begin() as db:
            await db.execute(
                text("UPDATE public.project_members SET role=:role WHERE id=:id"),
                {"role": role, "id": world["membership_id"]},
            )
            batch = await create_batch(
                db,
                project_id=world["project_id"],
                status="reviewing" if role == "reviewer" else "annotating",
            )
            batch.annotator_id = world["member_id"]
            batch.review_tasks = 1 if role == "reviewer" else 0
            task = await create_task(
                db,
                project_id=world["project_id"],
                status="review" if role == "reviewer" else "in_progress",
            )
            task.batch_id = batch.id
            task.reviewer_id = world["member_id"]
            task.assignee_id = world["member_id"]
            world.update(batch_id=batch.id, task_id=task.id)
            if role == "reviewer":
                reviewed = await create_task(
                    db, project_id=world["project_id"], status="completed"
                )
                reviewed.reviewer_id = world["member_id"]
                reviewed.reviewed_at = datetime.now(timezone.utc)
                world["reviewed_task_id"] = reviewed.id
        try:
            yield owner, api, world, url
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(Notification).where(
                        Notification.user_id.in_(world["actor_ids"])
                    )
                )
                await db.execute(
                    delete(AsyncJob).where(AsyncJob.user_id.in_(world["actor_ids"]))
                )
                await db.execute(
                    delete(Task).where(Task.project_id.in_(world["project_ids"]))
                )
                await db.execute(
                    delete(TaskBatch).where(
                        TaskBatch.project_id.in_(world["project_ids"])
                    )
                )


async def test_scope_reads_are_read_only_unlocked_and_observe_committed_revocation(
    test_engine,
):
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        actor = SimpleNamespace(id=world["member_id"], role="employee", is_active=True)
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            assert list(
                await db.scalars(membership_project_ids(actor, "reviewer"))
            ) == [world["project_id"]]
            async with owner.begin() as writer:
                await writer.execute(
                    text(
                        "SELECT id FROM public.projects WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": world["project_id"]},
                )
                await writer.execute(
                    text(
                        "SELECT id FROM public.project_members WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": world["membership_id"]},
                )
                await writer.execute(
                    text(
                        "UPDATE public.project_members SET role='annotator' WHERE id=:id"
                    ),
                    {"id": world["membership_id"]},
                )
            assert (
                list(await db.scalars(membership_project_ids(actor, "reviewer"))) == []
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await db.rollback()


@pytest.mark.parametrize("missing", [False, True])
async def test_membership_lookup_needs_only_actor_id(test_engine, missing):
    async with _consumer_world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            actor = SimpleNamespace(id=None if missing else world["member_id"])
            assert list(
                await db.scalars(membership_project_ids(actor, "reviewer"))
            ) == ([] if missing else [world["project_id"]])
            await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_standalone_membership_select_preserves_autoflush(test_engine, autoflush):
    async with _consumer_world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            db.autoflush = autoflush
            task_id = uuid.uuid4()
            pending = Task(
                id=task_id,
                project_id=world["project_id"],
                display_id="SCOPE-AF-" + task_id.hex[:12],
                file_name="pending.png",
                file_path=f"scope/{task_id}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            actor = SimpleNamespace(id=world["member_id"])
            assert list(
                await db.scalars(membership_project_ids(actor, "reviewer"))
            ) == [world["project_id"]]
            assert (pending in db.new) is (not autoflush)
            await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_standalone_owned_project_select_preserves_autoflush(
    test_engine, autoflush
):
    async with _consumer_world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            db.autoflush = autoflush
            task_id = uuid.uuid4()
            pending = Task(
                id=task_id,
                project_id=world["project_id"],
                display_id="SCOPE-AF-" + task_id.hex[:12],
                file_name="pending.png",
                file_path=f"scope/{task_id}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            actor = SimpleNamespace(
                id=world["owner_id"], role="project_admin", is_active=True
            )
            assert list(await db.scalars(owned_project_ids(actor))) == [
                world["project_id"]
            ]
            assert (pending in db.new) is (not autoflush)
            await db.rollback()


async def test_actor_membership_native_owner_and_grants_are_exact(test_engine):
    async with _consumer_world(test_engine) as (owner, _api, _world, _url):
        async with owner() as db:
            signature = "aap_workspace_v1.actor_project_memberships(uuid)"
            row = (
                await db.execute(
                    text(
                        "SELECT pg_get_userbyid(proowner),prosecdef,proisstrict,provolatile::text,proconfig FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
                    ),
                    {"signature": signature},
                )
            ).one()
            assert tuple(row[:4]) == ("workspace_code", True, True, "s")
            assert "search_path=pg_catalog, pg_temp" in row[4]
            for role, expected in (
                ("identity_api_consumer", True),
                ("legacy_runtime", True),
                ("append_client", False),
                ("assets_code", False),
                ("inference_code", False),
            ):
                assert (
                    await db.scalar(
                        text(
                            "SELECT has_function_privilege(:role,CAST(:signature AS regprocedure),'EXECUTE')"
                        ),
                        {"role": role, "signature": signature},
                    )
                    is expected
                )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                ),
                {"signature": signature},
            )


@pytest.mark.parametrize("dashboard", ["reviewer", "annotator"])
async def test_actual_dashboards_use_owner_relations_with_private_select_denied(
    test_engine, app_module, monkeypatch, dashboard
):
    from app.deps import get_db

    _configure_api_auth(test_engine, monkeypatch)
    monkeypatch.setattr(settings, "identity_briefs_database_url_file", "")
    monkeypatch.setattr(
        settings, "identity_briefs_database_url", project_query_briefs_dsn(test_engine)
    )

    async with _consumer_world(test_engine, role=dashboard) as (
        _owner,
        api,
        world,
        _url,
    ):

        async def request_db():
            async with api() as db:
                await _assert_private_denied(db)
                await db.rollback()
                await db.execute(text("SET TRANSACTION READ ONLY"))
                yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
        headers = {
            "Authorization": "Bearer "
            + create_access_token(subject=str(world["member_id"]), role="employee")
        }
        paths = (
            [
                "/api/v1/dashboard/reviewer",
                "/api/v1/dashboard/reviewer/today-mini",
                "/api/v1/dashboard/me/recent-reviews",
            ]
            if dashboard == "reviewer"
            else ["/api/v1/dashboard/annotator", "/api/v1/dashboard/annotator/batches"]
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            responses = [await client.get(path, headers=headers) for path in paths]
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text) for response in responses
        ]
        if dashboard == "reviewer":
            assert responses[0].json()["pending_review_count"] == 1
            assert responses[0].json()["pending_tasks"][0]["task_id"] == str(
                world["task_id"]
            )
            assert responses[2].json()[0]["task_id"] == str(world["reviewed_task_id"])
        else:
            assert responses[0].json()["assigned_tasks"] == 1
            assert responses[1].json()[0]["batch_id"] == str(world["batch_id"])


@pytest.mark.parametrize("actor", ["member", "owner"])
async def test_actual_job_and_notification_lists_use_owner_scope(test_engine, actor):
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        user_id = world["member_id"] if actor == "member" else world["owner_id"]
        async with owner.begin() as db:
            job = AsyncJob(
                id=uuid.uuid4(),
                kind="export",
                project_id=world["project_id"],
                user_id=user_id,
                status="completed",
                payload={},
                result={},
            )
            db.add(job)
            await db.flush()
            notice = Notification(
                id=uuid.uuid4(),
                user_id=user_id,
                type="job.completed",
                target_type="export",
                target_id=job.id,
                payload={"project_id": str(uuid.uuid4())},
            )
            db.add(notice)
            await db.flush()
            job_id, notice_id = job.id, notice.id
            for invalid_role in (None, "invalid"):
                foreign = await create_project(
                    db,
                    owner_id=world["owner_id"]
                    if actor == "member"
                    else world["member_id"],
                    name="not visible",
                )
                world["project_ids"].append(foreign.id)
                if invalid_role:
                    await create_membership(
                        db,
                        project_id=foreign.id,
                        user_id=user_id,
                        role=invalid_role,
                        assigned_by=world["owner_id"],
                    )
                denied_job = AsyncJob(
                    id=uuid.uuid4(),
                    kind="export",
                    project_id=foreign.id,
                    user_id=user_id,
                    status="completed",
                    payload={},
                    result={},
                )
                db.add(denied_job)
                await db.flush()
                db.add(
                    Notification(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        type="job.completed",
                        target_type="export",
                        target_id=denied_job.id,
                        payload={"project_id": str(world["project_id"])},
                    )
                )
        async with api() as db:
            await _assert_private_denied(db)
            await db.rollback()
            await db.execute(text("SET TRANSACTION READ ONLY"))
            result = await SqlAsyncJobReadAdapter(db).list_jobs(
                actor_id=user_id,
                status=None,
                kind=None,
                project_id=None,
                search=None,
                limit=20,
                offset=0,
            )
            assert result.total == 1
            assert result.items[0].id == job_id
            notices, total, unread = await NotificationService(db).list_for_user(
                user_id
            )
            assert total == unread == 1
            assert notices[0].id == notice_id
            await db.rollback()


async def test_node_reads_scope_relation_with_identity_profile_in_same_transaction(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, world, url):
        await _seed(owner, world, "employee", True)
        try:
            async with owner() as db:
                expected = list(
                    await db.scalars(
                        select(ProjectMember.project_id)
                        .where(
                            ProjectMember.user_id == world["member_id"],
                            ProjectMember.role == "reviewer",
                        )
                        .order_by(ProjectMember.project_id)
                    )
                )
            process = await asyncio.create_subprocess_exec(
                "node",
                str(Path(__file__).parent / "fixtures/workspace_actor_scope_peer.mjs"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    process.communicate(
                        json.dumps(
                            {
                                "database_url": url.set(
                                    drivername="postgresql"
                                ).render_as_string(hide_password=False),
                                "actor_id": str(world["member_id"]),
                                "project_id": str(world["project_id"]),
                                "expected": [str(value) for value in expected],
                            }
                        ).encode()
                    ),
                    timeout=20,
                )
                assert process.returncode == 0, err.decode()
                assert json.loads(out) == {
                    "ok": True,
                    "read_only": True,
                    "transaction_unchanged": True,
                }
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(Task).where(Task.project_id.in_(world["project_ids"]))
                )


@pytest.mark.parametrize("mode", ["collaborative_video", "owner_override"])
async def test_reviewer_claim_exceptions_preserve_owner_relation_semantics(
    test_engine, mode
):
    from app.api.v1.dashboard.reviewer import reviewer_dashboard

    async with _consumer_world(test_engine) as (owner, api, world, _url):
        actor_id = (
            world["member_id"] if mode == "collaborative_video" else world["owner_id"]
        )
        role = "employee" if mode == "collaborative_video" else "project_admin"
        async with owner.begin() as db:
            await db.execute(
                text("UPDATE public.task_batches SET status='archived' WHERE id=:id"),
                {"id": world["batch_id"]},
            )
            if mode == "collaborative_video":
                await db.execute(
                    text(
                        "UPDATE public.projects SET video_collaboration=CAST(:value AS jsonb) WHERE id=:id"
                    ),
                    {"id": world["project_id"], "value": '{"enabled":true}'},
                )
                await db.execute(
                    text(
                        "UPDATE public.tasks SET file_type='video',reviewer_id=:other,reviewer_claimed_at=NULL WHERE id=:id"
                    ),
                    {"id": world["task_id"], "other": world["owner_id"]},
                )
            else:
                await db.execute(
                    text(
                        "UPDATE public.tasks SET reviewer_claimed_at=now() WHERE id=:id"
                    ),
                    {"id": world["task_id"]},
                )
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            result = await reviewer_dashboard(
                db=db,
                current_user=SimpleNamespace(id=actor_id, role=role, is_active=True),
            )
            assert result.pending_review_count == 1
            assert result.pending_tasks[0].task_id == str(world["task_id"])
            await db.rollback()
