"""Observe live authority locks while a real no-op projection is blocked."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.security import create_access_token
from tests.labeling_class_query_helpers import (
    api_db_binding,
    clear_local_identity,
    make_request_runtime,
)
from tests.project_class_rename_owner_helpers import class_rename_binding
from tests.test_project_class_rename_legacy import _snapshot
from tests.test_workspace_class_rename_native import _cleanup, _seed


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _query_function_metadata(connection):
    return (
        (
            await connection.execute(
                text(
                    """SELECT function.oid::bigint AS oid,
                              function.proacl::text AS acl,
                              function.proowner::bigint AS owner,
                              pg_catalog.pg_get_functiondef(function.oid) AS definition
                       FROM pg_catalog.pg_proc AS function
                       JOIN pg_catalog.pg_namespace AS namespace
                         ON namespace.oid=function.pronamespace
                       WHERE namespace.nspname='aap_workspace_project_query_v1'
                         AND function.proname='project_query_step'"""
                )
            )
        )
        .mappings()
        .one()
    )


@asynccontextmanager
async def _gated_rename_projection(test_engine, *, project_id, lock_key):
    async with test_engine.connect() as owner:
        original = dict(await _query_function_metadata(owner))
    # Patch only this owned database function and exact owned Project. The gate
    # executes in the actual projection statement, without mocking authority,
    # the callback, its transaction, or any data read.
    needle = "BEGIN\n"
    assert needle in original["definition"]
    definition = original["definition"].replace(
        needle,
        "BEGIN\n"
        "    IF p_action='projection_row' "
        "AND p_request->>'operation'='get_project_projection' "
        f"AND p_request->>'target_id'='{project_id}' "
        "AND p_request #>> '{projection_evidence,source}'='class_rename' THEN\n"
        f"        PERFORM pg_catalog.pg_advisory_xact_lock({lock_key}::bigint);\n"
        "    END IF;\n",
        1,
    )
    try:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(definition)
            current = await _query_function_metadata(owner)
            for field in ("oid", "acl", "owner"):
                assert current[field] == original[field]
        yield
    finally:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(original["definition"])
            assert dict(await _query_function_metadata(owner)) == original


async def _wait_for_lock(test_engine, *, application_name=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    async with test_engine.connect() as observer:
        while loop.time() < deadline:
            await observer.execute(text("SELECT pg_catalog.pg_stat_clear_snapshot()"))
            statement = (
                "SELECT pid,application_name,wait_event_type,wait_event,query "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                "AND wait_event_type='Lock' "
            )
            args = {}
            if application_name is None:
                statement += (
                    "AND query ILIKE '%aap_workspace_project_query_v1.project_query_step%' "
                    "AND lower(wait_event)='advisory' "
                )
            else:
                statement += "AND application_name=:application_name "
                args["application_name"] = application_name
            statement += "ORDER BY query_start DESC LIMIT 1"
            row = (await observer.execute(text(statement), args)).mappings().first()
            if row is not None:
                return dict(row)
            await asyncio.sleep(0.03)
    raise AssertionError("the real database statement did not reach its lock gate")


async def _assert_update_blocked(test_engine, *, table, target):
    assert table in {"users", "projects"}
    async with test_engine.connect() as observer:
        transaction = await observer.begin()
        try:
            with pytest.raises(DBAPIError) as error:
                await observer.execute(
                    text(
                        f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": target},
                )
            sqlstate = getattr(error.value.orig, "sqlstate", None) or getattr(
                error.value.orig, "pgcode", None
            )
            assert sqlstate == "55P03"
        finally:
            await transaction.rollback()


@asynccontextmanager
async def _projection_gate(test_engine, lock_key):
    async with test_engine.connect() as gate:
        await gate.scalar(
            text("SELECT pg_catalog.pg_advisory_lock(:key)"), {"key": lock_key}
        )
        await gate.commit()
        try:
            yield gate
        finally:
            await gate.scalar(
                text("SELECT pg_catalog.pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            await gate.commit()


@pytest.mark.parametrize("mode", ("borrowed", "dedicated", "http"))
async def test_noop_projection_keeps_account_and_project_fences_until_result_is_ready(
    test_engine, app_module, monkeypatch, tmp_path, mode
):
    maker, world = await _seed(test_engine)
    runtime = None
    request_task = None
    revoke_task = None
    lock_key = int(uuid.uuid4().hex[:15], 16)
    label = f"rename-revoke-{uuid.uuid4().hex}"
    before = await _snapshot(maker, world["project_id"])
    clear_local_identity(monkeypatch)
    token = create_access_token(subject=str(world["owner_id"]), role="project_admin")

    async def revoke_actor():
        async with maker.begin() as writer:
            await writer.execute(
                text("SELECT pg_catalog.set_config('application_name',:label,true)"),
                {"label": label},
            )
            await writer.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": world["owner_id"]},
            )

    try:
        runtime, runtime_maker = await make_request_runtime(
            test_engine, application_name=f"rename-fence-api-{mode}"
        )
        async with class_rename_binding(
            monkeypatch, test_engine, mode=mode, tmp_path=tmp_path
        ):
            async with api_db_binding(app_module, runtime_maker):
                async with _gated_rename_projection(
                    test_engine, project_id=world["project_id"], lock_key=lock_key
                ):
                    async with _projection_gate(test_engine, lock_key) as gate:
                        transport = httpx.ASGITransport(
                            app=app_module, raise_app_exceptions=False
                        )
                        async with httpx.AsyncClient(
                            transport=transport, base_url="http://class-rename-fences"
                        ) as client:
                            url = (
                                f"/api/v1/projects/{world['project_id']}/classes/rename"
                            )
                            body = {
                                "old_name": " Car ",
                                "new_name": "Car",
                                "tool_unit_id": "__unknown_unit_ignored_for_noop__",
                            }
                            headers = {"Authorization": f"Bearer {token}"}
                            request_task = asyncio.create_task(
                                client.post(url, headers=headers, json=body)
                            )
                            blocked = await _wait_for_lock(test_engine)
                            assert not request_task.done(), blocked
                            await _assert_update_blocked(
                                test_engine, table="users", target=world["owner_id"]
                            )
                            await _assert_update_blocked(
                                test_engine,
                                table="projects",
                                target=world["project_id"],
                            )
                            revoke_task = asyncio.create_task(revoke_actor())
                            waiting_revoke = await _wait_for_lock(
                                test_engine, application_name=label
                            )
                            assert not revoke_task.done(), waiting_revoke
                            unlocked = await gate.scalar(
                                text("SELECT pg_catalog.pg_advisory_unlock(:key)"),
                                {"key": lock_key},
                            )
                            assert unlocked is True
                            await gate.commit()
                            response = await asyncio.wait_for(request_task, timeout=30)
                            assert response.status_code == 200, response.text
                            assert (
                                response.json()["tool_bindings"]["bbox"]["classes"][0][
                                    "name"
                                ]
                                == "Car"
                            )
                            await asyncio.wait_for(revoke_task, timeout=10)
                            assert await _snapshot(maker, world["project_id"]) == before
                            denied = await client.post(url, headers=headers, json=body)
                            assert denied.status_code == 401, denied.text
                            assert denied.headers["www-authenticate"] == "Bearer"
    finally:
        for pending in (request_task, revoke_task):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        if runtime is not None:
            await runtime.dispose()
        await _cleanup(maker, world)
