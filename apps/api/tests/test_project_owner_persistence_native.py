"""Project set locks and counter writes through the actual restricted API LOGIN."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.batch import BatchService
from app.services.labeling_tasks import LabelingTaskService
from app.services.project_access import get_workspace_project
from app.services.project_workspace import lock_project_references_for_update
from tests.factory import create_project
from tests.test_labeling_project_counter_owner_native import _seed_counters
from tests.test_workspace_project_authority_locking import _world
from tests.test_workspace_project_access_step_native import _assert_nowait_conflict

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]
_ROLE = "identity_api_consumer"
_COUNTER = "aap_labeling_v1.write_project_counter_projection(uuid,jsonb)"
_LOCK = "aap_workspace_v1.lock_project_references_for_update(uuid[])"


@asynccontextmanager
async def _restricted(test_engine, *, counters=False):
    async with _world(test_engine) as (owner, api, world, url):
        async with owner.begin() as db:
            before = await db.scalar(
                text(
                    "SELECT relacl::text FROM pg_class WHERE oid='public.projects'::regclass"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT has_any_column_privilege(:role,'public.projects','UPDATE')"
                ),
                {"role": _ROLE},
            )
        try:
            batch_ids = await _seed_counters(owner, world) if counters else []
            yield owner, api, world, url, batch_ids
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(Task).where(Task.project_id == world["project_id"])
                )
                await db.execute(
                    delete(TaskBatch).where(TaskBatch.project_id == world["project_id"])
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT relacl::text FROM pg_class WHERE oid='public.projects'::regclass"
                        )
                    )
                    == before
                )


async def _private_denied(db):
    assert await db.scalar(text("SELECT current_user")) == _ROLE
    for privilege, sql in (
        ("SELECT", "SELECT id FROM public.projects LIMIT 1"),
        ("UPDATE", "UPDATE public.projects SET total_tasks=0 WHERE false"),
    ):
        assert not await db.scalar(
            text(
                "SELECT has_any_column_privilege(current_user,'public.projects',:privilege)"
            ),
            {"privilege": privilege},
        )
        with pytest.raises(DBAPIError) as rejected:
            async with db.begin_nested():
                await db.execute(text(sql))
        assert rejected.value.orig.sqlstate == "42501"


async def _row(db, project_id):
    with db.no_autoflush:
        raw = await db.scalar(
            text("SELECT aap_workspace_v1.read_project_row(CAST(:id AS uuid))"),
            {"id": project_id},
        )
    return json.loads(raw) if raw is not None else None


async def _recount(db, method, project_id):
    if method == "batch":
        await BatchService(db)._sync_project_counters(project_id)
    else:
        await LabelingTaskService(db).recalculate_source_less_task_counters(project_id)


async def test_new_native_functions_have_exact_owners_and_execute_grants(test_engine):
    async with _restricted(test_engine) as (owner, _api, _world, _url, _batches):
        async with owner() as db:
            for signature, owner_name in (
                (_LOCK, "workspace_code"),
                (_COUNTER, "labeling_code"),
            ):
                row = (
                    await db.execute(
                        text(
                            "SELECT pg_get_userbyid(proowner),prosecdef,provolatile::text,proconfig "
                            "FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
                        ),
                        {"signature": signature},
                    )
                ).one()
                assert tuple(row[:3]) == (owner_name, True, "v")
                assert "search_path=pg_catalog, pg_temp" in row[3]
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a "
                        "WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                    ),
                    {"signature": signature},
                )
                for role, expected in (
                    (_ROLE, True),
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
                    ), (signature, role)


async def test_sorted_set_lock_uses_same_transaction_and_existing_subset(test_engine):
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        async with owner.begin() as db:
            second = await create_project(db, owner_id=world["owner_id"])
            world["project_ids"].append(second.id)
        requested = [second.id, world["project_id"], second.id, uuid.uuid4()]
        async with api() as db:
            await _private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            assert await lock_project_references_for_update(db, requested) == sorted(
                set(requested[:2])
            )
            assert await lock_project_references_for_update(db, []) == []
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for project_id in requested[:2]:
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.projects WHERE id=$1 FOR UPDATE NOWAIT",
                    project_id,
                )
            await db.rollback()
        async with owner.begin() as db:
            assert (
                len(
                    (
                        await db.execute(
                            text(
                                "SELECT id FROM public.projects WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE NOWAIT"
                            ),
                            {"ids": requested[:2]},
                        )
                    ).all()
                )
                == 2
            )


async def test_batch_project_set_contention_preserves_409_and_full_rollback(
    test_engine,
):
    async with _restricted(test_engine, counters=True) as (
        owner,
        api,
        world,
        _url,
        batches,
    ):
        async with owner() as holder, api() as db:
            await holder.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": world["project_id"]},
            )
            pending_id = uuid.uuid4()
            db.add(
                Dataset(
                    id=pending_id,
                    display_id="DS-" + pending_id.hex[:16],
                    name="Must roll back after Project contention",
                    created_by=world["owner_id"],
                )
            )
            with pytest.raises(HTTPException) as rejected:
                await BatchService(db)._lock_decision_scope(
                    actor_id=world["owner_id"], batch_ids=batches
                )
            assert rejected.value.status_code == 409
            assert rejected.value.detail == {"reason": "batch_decision_busy"}
            assert not db.in_transaction()
            await holder.rollback()
        async with owner() as db:
            assert await db.get(Dataset, pending_id) is None
            assert (
                await db.scalar(
                    text("SELECT name FROM public.task_batches WHERE id=:id"),
                    {"id": batches[0]},
                )
                == "counter"
            )


@pytest.mark.parametrize("method", ["batch", "source_less"])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_counter_consumers_work_without_project_select_or_update(
    test_engine, method, autoflush
):
    async with _restricted(test_engine, counters=True) as (
        owner,
        api,
        world,
        _url,
        _batches,
    ):
        async with api() as db:
            await _private_denied(db)
            db.autoflush = autoflush
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            await _recount(db, method, world["project_id"])
            assert (await _row(db, world["project_id"]))["total_tasks"] == (
                9 if method == "source_less" and not autoflush else 2
            )
            await db.flush()
            row = await _row(db, world["project_id"])
            assert [
                row[key]
                for key in (
                    "total_tasks",
                    "completed_tasks",
                    "review_tasks",
                    "in_progress_tasks",
                )
            ] == [2, 1, 0, 1]
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            if method == "source_less":
                batches = (
                    await db.execute(
                        text(
                            "SELECT b.total_tasks,b.completed_tasks,b.review_tasks,count(t.id),"
                            "count(t.id) FILTER (WHERE t.status='completed'),count(t.id) FILTER (WHERE t.status='review') "
                            "FROM aap_labeling_v1.batch_rows b LEFT JOIN aap_labeling_v1.task_rows t ON t.batch_id=b.id "
                            "WHERE b.project_id=:id GROUP BY b.id,b.total_tasks,b.completed_tasks,b.review_tasks"
                        ),
                        {"id": world["project_id"]},
                    )
                ).all()
                assert len(batches) == 2
                assert all(tuple(row[:3]) == tuple(row[3:]) for row in batches)
            async with owner() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT total_tasks FROM public.projects WHERE id=:id"),
                        {"id": world["project_id"]},
                    )
                    == 9
                )
            await db.rollback()
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT total_tasks FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == 9
            )


@pytest.mark.parametrize("ack", ["missing", "text", "wrong_uuid"])
async def test_bad_counter_ack_rolls_back_actual_native_write_and_session_recovers(
    test_engine, monkeypatch, ack
):
    from app.services import labeling_project_counter_data as adapter

    async with _restricted(test_engine, counters=True) as (
        _owner,
        api,
        world,
        _url,
        _batches,
    ):
        native = "aap_labeling_v1.write_project_counter_projection(CAST(:project_id AS uuid),CAST(:patch AS jsonb))"
        expression = {
            "missing": f"NULLIF({native},CAST(:project_id AS uuid))",
            "text": f"({native})::text",
            "wrong_uuid": f"CASE WHEN {native} IS NOT NULL THEN gen_random_uuid() END",
        }[ack]
        async with api() as db:
            with monkeypatch.context() as patch:
                patch.setattr(
                    adapter, "_WRITE_COUNTER_PROJECTION", text("SELECT " + expression)
                )
                with pytest.raises(
                    StaleDataError if ack == "missing" else HTTPException
                ) as rejected:
                    await _recount(db, "batch", world["project_id"])
                if ack != "missing":
                    assert rejected.value.status_code == 503
                assert not db.is_active
                await db.rollback()
            assert (await _row(db, world["project_id"]))["total_tasks"] == 9
            await _recount(db, "batch", world["project_id"])
            assert (await _row(db, world["project_id"]))["total_tasks"] == 2
            await db.rollback()


@pytest.mark.parametrize("method", ["batch", "source_less"])
async def test_deleted_cached_counter_target_raises_stale_data_inside_flush(
    test_engine, method
):
    async with _restricted(test_engine) as (owner, api, world, _url, _batches):
        async with api() as db:
            cached = await get_workspace_project(db, project_id=world["project_id"])
            cached.total_tasks = 9
            async with owner.begin() as external:
                await external.execute(
                    delete(Project).where(Project.id == world["project_id"])
                )
            with pytest.raises(StaleDataError):
                await _recount(db, method, world["project_id"])
                await db.flush()
            assert not db.is_active
            assert cached is not None
            await db.rollback()


@pytest.mark.parametrize("end", ["rollback", "close"])
async def test_counter_opt_in_does_not_survive_abandoned_session_transaction(
    test_engine, end
):
    async with _restricted(test_engine, counters=True) as (
        _owner,
        api,
        world,
        _url,
        _batches,
    ):
        async with api() as db:
            db.autoflush = False
            await _recount(db, "source_less", world["project_id"])
            await getattr(db, end)()
            project = await get_workspace_project(db, project_id=world["project_id"])
            project.total_tasks = 123
            with pytest.raises(DBAPIError) as rejected:
                await db.flush()
            assert rejected.value.orig.sqlstate == "42501"
            await db.rollback()


async def test_node_calls_both_native_capabilities_without_python_provider(test_engine):
    async with _restricted(test_engine) as (_owner, _api, world, url, _batches):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/project_owner_persistence_peer.mjs"),
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
                            "project_id": str(world["project_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {
                "ok": True,
                "transaction_unchanged": True,
                "rollback": True,
            }
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize(
    "patch",
    [None, [], {}, {"name": "forbidden"}, {"updated_at": "2001-01-01T00:00:00Z"}],
)
async def test_counter_native_rejects_out_of_contract_patch(test_engine, patch):
    async with _restricted(test_engine) as (_owner, api, world, _url, _batches):
        async with api() as db:
            with pytest.raises(DBAPIError) as rejected:
                await db.scalar(
                    text(
                        "SELECT aap_labeling_v1.write_project_counter_projection(CAST(:id AS uuid),CAST(:patch AS jsonb))"
                    ),
                    {
                        "id": world["project_id"],
                        "patch": None if patch is None else json.dumps(patch),
                    },
                )
            assert rejected.value.orig.sqlstate == "22023"
            await db.rollback()


async def test_counter_native_writes_only_supplied_columns_and_returns_missing_null(
    test_engine,
):
    async with _restricted(test_engine, counters=True) as (
        _owner,
        api,
        world,
        _url,
        _batches,
    ):
        async with api() as db:
            statement = text(
                "SELECT aap_labeling_v1.write_project_counter_projection(CAST(:id AS uuid),CAST(:patch AS jsonb))"
            )
            assert (
                await db.scalar(
                    statement,
                    {
                        "id": world["project_id"],
                        "patch": json.dumps(
                            {"total_tasks": 5, "updated_at": "2001-01-01T00:00:00Z"}
                        ),
                    },
                )
                == world["project_id"]
            )
            row = await _row(db, world["project_id"])
            assert [
                row[key]
                for key in (
                    "total_tasks",
                    "completed_tasks",
                    "review_tasks",
                    "in_progress_tasks",
                )
            ] == [5, 7, 6, 8]
            assert json.loads(row["batch_summary"]) == {"old": 1}
            assert datetime.fromisoformat(row["updated_at"]) == datetime(
                2001, 1, 1, tzinfo=timezone.utc
            )
            assert (
                await db.scalar(
                    statement, {"id": uuid.uuid4(), "patch": '{"total_tasks":0}'}
                )
                is None
            )
            await db.rollback()


@pytest.mark.parametrize("operation", ["lock", "counter"])
async def test_project_write_capabilities_refuse_read_only_transaction(
    test_engine, operation
):
    async with _restricted(test_engine) as (_owner, api, world, _url, _batches):
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            with pytest.raises(DBAPIError) as rejected:
                if operation == "lock":
                    await lock_project_references_for_update(db, [world["project_id"]])
                else:
                    await db.scalar(
                        text(
                            "SELECT aap_labeling_v1.write_project_counter_projection(CAST(:id AS uuid),CAST(:patch AS jsonb))"
                        ),
                        {"id": world["project_id"], "patch": '{"total_tasks":0}'},
                    )
            assert rejected.value.orig.sqlstate == "25006"
            await db.rollback()
