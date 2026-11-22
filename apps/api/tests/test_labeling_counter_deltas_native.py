"""Native lifecycle counter behavior through the real restricted API role."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import NoResultFound

from app.db.models.project import Project
from app.db.models.task import Task
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.labeling_tasks import LabelingTaskService
from app.services.project_access import get_workspace_project
from tests.factory import create_task
from tests.frozen_namespaces import current
from tests.test_project_owner_persistence_native import _private_denied, _restricted

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_STAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
_METHODS = {
    "review": "decrement_project_review_count",
    "completed": "decrement_project_completed_count",
    "approval": "record_project_review_approval",
}


async def _seed_review_task(owner, world):
    async with owner.begin() as db:
        task = await create_task(db, project_id=world["project_id"], status="review")
        task.assignee_id = world["member_id"]
        task.review_round_id = uuid.uuid4()
        return task.id


async def _seed(owner, world, *, completed=11, review=7):
    async with owner.begin() as db:
        await db.execute(
            text(
                "UPDATE public.projects SET completed_tasks=:completed,review_tasks=:review,"
                "updated_at=:stamp WHERE id=:id"
            ),
            {
                "id": world["project_id"],
                "completed": completed,
                "review": review,
                "stamp": _STAMP,
            },
        )


async def _row(db, project_id):
    with db.no_autoflush:
        return (
            await db.execute(
                text(
                    "SELECT counters.completed_tasks,counters.review_tasks,facts.updated_at "
                    "FROM yoao_labeling_v1.project_catalog_counters AS counters "
                    "JOIN yoao_workspace_v1.project_facts AS facts ON facts.id=counters.project_id "
                    "WHERE facts.id=:id"
                ),
                {"id": project_id},
            )
        ).one()


async def test_native_counter_function_contract_and_exact_privileges(test_engine):
    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "alembic/sql/0299_labeling_project_counter_deltas.schema.json"
        ).read_text()
    )
    async with _restricted(test_engine) as (owner, _api, _world, _url, _batches):
        async with owner() as db:
            for function in manifest["functions"]:
                signature = current(function["signature"])
                row = (
                    await db.execute(
                        text(
                            "SELECT pg_get_userbyid(p.proowner),l.lanname,p.prosecdef,"
                            "p.provolatile::text,p.proparallel::text,p.proisstrict,p.proconfig,"
                            "pg_get_function_result(p.oid),pg_get_function_arguments(p.oid) "
                            "FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang "
                            "WHERE p.oid=CAST(:signature AS regprocedure)"
                        ),
                        {"signature": signature},
                    )
                ).one()
                assert tuple(row[:6]) == ("labeling_code", "sql", True, "v", "u", False)
                assert row[6] == ["search_path=pg_catalog, pg_temp"]
                assert row[7] == (
                    "TABLE(completed_tasks integer, review_tasks integer)"
                    if "approval" in signature
                    else "integer"
                )
                assert row[8] == "p_project_id uuid"
                assert function["strict"] is False
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_proc p,LATERAL aclexplode(p.proacl) a "
                        "WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 "
                        "AND a.privilege_type='EXECUTE')"
                    ),
                    {"signature": signature},
                )
                for role, expected in (
                    ("identity_api_consumer", True),
                    ("legacy_runtime", True),
                    ("yoao_append_client", False),
                    ("assets_code", False),
                    ("inference_code", False),
                    ("workspace_code", False),
                ):
                    assert (
                        await db.scalar(
                            text(
                                "SELECT has_function_privilege(:role,CAST(:signature AS regprocedure),'EXECUTE')"
                            ),
                            {"role": role, "signature": signature},
                        )
                        is expected
                    ), (signature, role)


@pytest.mark.parametrize("method", _METHODS)
async def test_restricted_counter_and_task_share_outer_rollback(test_engine, method):
    expected = {"review": (11, 6), "completed": (10, 7), "approval": (12, 6)}[method]
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        task_id = await _seed_review_task(owner, world)
        await _seed(owner, world)
        async with api() as db:
            await _private_denied(db)
            project = await get_workspace_project(db, project_id=world["project_id"])
            task = await get_core_row(db, task_row, task_id)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current(),now()"))
            ).one()
            service = LabelingTaskService(db)
            await service.withdraw_task(task, actor_id=world["member_id"])
            await getattr(service, _METHODS[method])(project)
            assert (project.completed_tasks, project.review_tasks) == expected
            assert tuple(await _row(db, project.id)) == (*expected, before[2])
            assert (
                await db.scalar(select(task_row.status).where(task_row.id == task_id))
                == "in_progress"
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current(),now()"))
            ).one() == before
            await db.rollback()
        async with owner() as check:
            assert tuple(await _row(check, world["project_id"])) == (11, 7, _STAMP)
            assert (await check.get(Task, task_id)).status == "review"


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("kind", ["null", "missing"])
async def test_restricted_missing_project_preserves_scalar_or_zero_rows(
    test_engine, method, kind
):
    async with _restricted(test_engine) as (_owner, api, _world, _url, _batches):
        async with api() as db:
            project = Project(
                id=None if kind == "null" else uuid.uuid4(),
                completed_tasks=99,
                review_tasks=77,
            )
            if method == "approval":
                with pytest.raises(NoResultFound):
                    await LabelingTaskService(db).record_project_review_approval(
                        project
                    )
                assert (project.completed_tasks, project.review_tasks) == (99, 77)
            else:
                assert (
                    await getattr(LabelingTaskService(db), _METHODS[method])(project)
                    is None
                )
                assert (
                    getattr(
                        project,
                        "review_tasks" if method == "review" else "completed_tasks",
                    )
                    is None
                )
            await db.rollback()


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("kind", ["existing", "missing", "null"])
async def test_restricted_counter_attempt_rejected_in_read_only_transaction(
    test_engine, method, kind
):
    async with _restricted(test_engine) as (_owner, api, world, _url, _batches):
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            project = Project(
                id={
                    "existing": world["project_id"],
                    "missing": uuid.uuid4(),
                    "null": None,
                }[kind]
            )
            with pytest.raises(DBAPIError) as rejected:
                await getattr(LabelingTaskService(db), _METHODS[method])(project)
            assert rejected.value.orig.sqlstate == "25006"
            await db.rollback()


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("initial", [0, -2])
async def test_restricted_counter_numeric_edges(test_engine, method, initial):
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        await _seed(owner, world, completed=initial, review=initial)
        async with api.begin() as db:
            project = await get_workspace_project(db, project_id=world["project_id"])
            await getattr(LabelingTaskService(db), _METHODS[method])(project)
        async with owner() as check:
            expected = {
                "review": (initial, 0),
                "completed": (0, initial),
                "approval": (initial + 1, 0),
            }[method]
            assert tuple((await _row(check, world["project_id"]))[:2]) == expected


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("review", "review", (2, 0)),
        ("completed", "completed", (0, 2)),
        ("approval", "approval", (4, 0)),
        ("approval", "review", (3, 0)),
    ],
)
async def test_restricted_concurrent_deltas_wait_without_losing_updates(
    test_engine, first, second, expected
):
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        await _seed(owner, world, completed=2, review=2)
        async with api() as writer, api() as contender:
            a = await get_workspace_project(writer, project_id=world["project_id"])
            b = await get_workspace_project(contender, project_id=world["project_id"])
            pid = await contender.scalar(text("SELECT pg_backend_pid()"))
            await getattr(LabelingTaskService(writer), _METHODS[first])(a)
            pending = asyncio.create_task(
                getattr(LabelingTaskService(contender), _METHODS[second])(b)
            )
            try:
                async with owner() as check:
                    async with asyncio.timeout(5):
                        while not await check.scalar(
                            text(
                                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": pid},
                        ):
                            assert not pending.done()
                            await asyncio.sleep(0.01)
                await writer.commit()
                await asyncio.wait_for(pending, 5)
                await contender.commit()
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await writer.rollback()
                await contender.rollback()
        async with owner() as check:
            row = await _row(check, world["project_id"])
            assert tuple(row[:2]) == expected


@pytest.mark.parametrize("method", _METHODS)
async def test_restricted_overflow_rolls_back_task_and_recovers_next_transaction(
    test_engine, method
):
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        task_id = await _seed_review_task(owner, world)
        completed = (
            2147483647
            if method == "approval"
            else -2147483648
            if method == "completed"
            else 11
        )
        review = -2147483648 if method == "review" else 7
        await _seed(owner, world, completed=completed, review=review)
        async with api() as db:
            project = await get_workspace_project(db, project_id=world["project_id"])
            task = await get_core_row(db, task_row, task_id)
            await LabelingTaskService(db).withdraw_task(
                task, actor_id=world["member_id"]
            )
            assert (
                await db.scalar(select(task_row.status).where(task_row.id == task_id))
                == "in_progress"
            )
            with pytest.raises(DBAPIError) as rejected:
                await getattr(LabelingTaskService(db), _METHODS[method])(project)
            assert rejected.value.orig.sqlstate == "22003"
            await db.rollback()
        async with owner() as check:
            assert tuple(await _row(check, world["project_id"])) == (
                completed,
                review,
                _STAMP,
            )
            assert (await check.get(Task, task_id)).status == "review"
        await _seed(owner, world)
        async with api.begin() as db:
            project = await get_workspace_project(db, project_id=world["project_id"])
            await getattr(LabelingTaskService(db), _METHODS[method])(project)


async def test_node_peer_uses_native_deltas_and_rolls_back_same_transaction(
    test_engine,
):
    async with _restricted(test_engine) as (owner, _api, world, url, _batches):
        await _seed(owner, world)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/labeling_counter_deltas_peer.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, errors = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {
                            "database_url": url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "project_id": str(world["project_id"]),
                        }
                    ).encode()
                ),
                20,
            )
            assert process.returncode == 0, errors.decode()
            assert json.loads(output) == {
                "ok": True,
                "transaction_unchanged": True,
                "rollback": True,
            }
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        async with owner() as check:
            assert tuple(await _row(check, world["project_id"])) == (11, 7, _STAMP)
