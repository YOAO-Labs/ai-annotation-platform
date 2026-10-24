"""PostgreSQL tests for the fixed Jobs row-lock capabilities."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import re

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from aap_jobs.lifecycle_contract import (
    ASYNC_JOB_FUNCTION_ROW_TYPES,
    ASYNC_JOB_ROW_COLUMNS,
)
from aap_jobs.row_lock_contract import JOBS_ROW_LOCK_FUNCTIONS
from app.db.models.async_job import AsyncJob
from app.services import async_job as async_job_service
from app.services.jobs_lifecycle_data import job_lock_statement
from tests.test_jobs_list_query_native import _committed_identity_scope_world


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if value:
            return value
        current = getattr(current, "orig", None) or current.__cause__
    return None


@contextmanager
def _capture_sql(session):
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _assert_uses_only_lock_function(statements: list[str], function: str) -> None:
    lowered = [statement.lower() for statement in statements]
    assert any(f"aap_jobs_v1.{function}(" in statement for statement in lowered)
    assert not any(
        re.search(r"\bfrom\s+(?:public\.)?async_jobs\b", statement)
        for statement in lowered
    )


@pytest.mark.asyncio
async def test_row_lock_functions_have_fixed_definer_and_exact_row_abi(db_session):
    expected_columns = [
        f"{name}:{ASYNC_JOB_FUNCTION_ROW_TYPES[name]}" for name in ASYNC_JOB_ROW_COLUMNS
    ]
    for function in JOBS_ROW_LOCK_FUNCTIONS.values():
        metadata = (
            (
                await db_session.execute(
                    text(
                        "SELECT pg_catalog.pg_get_userbyid(p.proowner) AS owner, "
                        "p.prosecdef AS security_definer, "
                        "p.provolatile::text AS volatility, "
                        "p.proconfig AS settings, "
                        "p.proretset AS returns_set, "
                        "(SELECT pg_catalog.array_agg("
                        "p.proargnames[position] || ':' || "
                        "pg_catalog.format_type(p.proallargtypes[position], NULL) "
                        "ORDER BY position) "
                        "FROM pg_catalog.generate_subscripts(p.proargnames, 1) "
                        "AS argument(position) "
                        "WHERE p.proargmodes[position] = 't') AS out_columns, "
                        "EXISTS (SELECT 1 "
                        "FROM pg_catalog.aclexplode(COALESCE("
                        "p.proacl, pg_catalog.acldefault('f', p.proowner))) acl "
                        "WHERE acl.grantee = 0 "
                        "AND acl.privilege_type = 'EXECUTE') AS public_execute "
                        "FROM pg_catalog.pg_proc AS p "
                        "WHERE p.oid = CAST(:signature AS regprocedure)"
                    ),
                    {"signature": f"{function}(uuid)"},
                )
            )
            .mappings()
            .one()
        )
        assert metadata["owner"] == "jobs_code"
        assert metadata["security_definer"] is True
        assert metadata["volatility"] == "v"
        assert metadata["settings"] == ["search_path=pg_catalog, pg_temp"]
        assert metadata["returns_set"] is True
        assert metadata["out_columns"] == expected_columns
        assert metadata["public_execute"] is False
        assert (
            await db_session.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    "'jobs_lifecycle_client', "
                    "CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": f"{function}(uuid)"},
            )
        ) is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned API role and disposable database",
)
async def test_restricted_api_can_lock_job_without_private_select_or_update(
    db_session,
):
    consumer_url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    target = db_session.bind.engine.url
    assert (consumer_url.host, consumer_url.port, consumer_url.database) == (
        target.host,
        target.port,
        target.database,
    )
    assert consumer_url.database and consumer_url.database.endswith("_test")
    assert consumer_url.username == "identity_api_consumer"

    lifecycle_lock_rights = (
        await db_session.execute(
            text(
                "SELECT pg_catalog.has_function_privilege("
                "'jobs_lifecycle_client', "
                "'aap_jobs_v1.read_job_for_update(uuid)'::regprocedure, "
                "'EXECUTE'), "
                "pg_catalog.has_function_privilege("
                "'jobs_lifecycle_client', "
                "'aap_jobs_v1.read_job_for_share_nowait(uuid)'::regprocedure, "
                "'EXECUTE')"
            )
        )
    ).one()
    assert lifecycle_lock_rights == (False, False)

    job = await async_job_service.create_job(
        db_session,
        kind="audit_archive",
        payload={"lock": "same transaction"},
    )
    job_id = job.id
    await db_session.flush()
    db_session.expunge(job)

    # The owner test transaction restores this ACL change during fixture rollback.
    await db_session.execute(
        text("REVOKE SELECT, UPDATE ON public.async_jobs FROM identity_api_consumer")
    )
    await db_session.execute(text("SET LOCAL ROLE identity_api_consumer"))
    try:
        rights = (
            await db_session.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'public.async_jobs', 'SELECT'), "
                    "pg_catalog.has_table_privilege(current_user, "
                    "'public.async_jobs', 'UPDATE'), "
                    "pg_catalog.has_any_column_privilege(current_user, "
                    "'public.async_jobs', 'SELECT'), "
                    "pg_catalog.has_any_column_privilege(current_user, "
                    "'public.async_jobs', 'UPDATE')"
                )
            )
        ).one()
        assert rights == (False, False, False, False)

        with pytest.raises(DBAPIError) as denied_select:
            async with db_session.begin_nested():
                await db_session.execute(
                    text("SELECT id FROM public.async_jobs WHERE id=:job_id"),
                    {"job_id": job_id},
                )
        assert _sqlstate(denied_select.value) == "42501"

        with pytest.raises(DBAPIError) as denied_update:
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "UPDATE public.async_jobs SET error_message='private write' "
                        "WHERE id=:job_id"
                    ),
                    {"job_id": job_id},
                )
        assert _sqlstate(denied_update.value) == "42501"

        with _capture_sql(db_session) as statements:
            result = await db_session.execute(job_lock_statement(job_id, mode="update"))
            row = result.scalar_one()

        assert row.id == job_id
        assert row.kind == "audit_archive"
        assert row.payload == {"lock": "same transaction"}
        _assert_uses_only_lock_function(statements, "read_job_for_update")
    finally:
        await db_session.execute(text("RESET ROLE"))


@pytest.mark.asyncio
async def test_share_locks_are_compatible_and_conflict_with_update_nowait(test_engine):
    async with _committed_identity_scope_world(test_engine) as world:
        sessions = world["sessions"]
        job_id = world["project_job_id"]

        async with sessions() as first_share:
            await first_share.begin()
            first_row = await first_share.scalar(
                job_lock_statement(job_id, mode="share_nowait")
            )
            assert first_row is not None and first_row.id == job_id

            async with sessions() as second_share:
                await second_share.begin()
                second_row = await second_share.scalar(
                    job_lock_statement(job_id, mode="share_nowait")
                )
                assert second_row is not None and second_row.id == job_id
                await second_share.commit()
            await first_share.rollback()

        async with sessions() as update_holder:
            await update_holder.begin()
            locked = await update_holder.scalar(
                job_lock_statement(job_id, mode="update")
            )
            assert locked is not None and locked.id == job_id

            async with sessions() as share_attempt:
                with pytest.raises(DBAPIError) as conflict:
                    async with share_attempt.begin():
                        await share_attempt.scalar(
                            job_lock_statement(job_id, mode="share_nowait")
                        )
                assert _sqlstate(conflict.value) == "55P03"
            await update_holder.rollback()


@pytest.mark.asyncio
async def test_update_lock_waits_and_returns_the_committed_current_row(test_engine):
    async with _committed_identity_scope_world(test_engine) as world:
        sessions = world["sessions"]
        job_id = world["project_job_id"]
        holder = sessions()
        waiter_started = asyncio.Event()
        waiter_pid = asyncio.get_running_loop().create_future()
        waiter_task = None
        lock_query_sent = asyncio.Event()
        engine = test_engine.sync_engine

        def observe_lock_query(
            _connection, _cursor, statement, _parameters, _context, _many
        ):
            if "aap_jobs_v1.read_job_for_update(" in statement.lower():
                lock_query_sent.set()

        event.listen(engine, "before_cursor_execute", observe_lock_query)
        try:
            await holder.begin()
            locked = await holder.scalar(job_lock_statement(job_id, mode="update"))
            assert locked is not None
            await holder.execute(
                text(
                    "UPDATE public.async_jobs SET error_message=:value WHERE id=:job_id"
                ),
                {"value": "committed after lock wait", "job_id": job_id},
            )
            lock_query_sent.clear()

            async def wait_for_update_lock():
                async with sessions() as waiting:
                    async with waiting.begin():
                        pid = await waiting.scalar(text("SELECT pg_backend_pid()"))
                        waiter_pid.set_result(pid)
                        waiter_started.set()
                        row = await waiting.scalar(
                            job_lock_statement(
                                job_id,
                                mode="update",
                                populate_existing=True,
                            )
                        )
                        assert row is not None
                        return row.error_message

            waiter_task = asyncio.create_task(wait_for_update_lock())
            await asyncio.wait_for(waiter_started.wait(), timeout=3)
            await asyncio.wait_for(lock_query_sent.wait(), timeout=3)
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                wait_event_type = await holder.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_catalog.pg_stat_activity "
                        "WHERE pid=:pid"
                    ),
                    {"pid": waiter_pid.result()},
                )
                if wait_event_type == "Lock":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("the competing Jobs update lock did not wait")
            assert not waiter_task.done()

            await holder.commit()
            assert await asyncio.wait_for(waiter_task, timeout=5) == (
                "committed after lock wait"
            )
        finally:
            if waiter_task is not None and not waiter_task.done():
                waiter_task.cancel()
            if waiter_task is not None:
                try:
                    await waiter_task
                except asyncio.CancelledError:
                    pass
            if holder.in_transaction():
                await holder.rollback()
            await holder.close()
            event.remove(engine, "before_cursor_execute", observe_lock_query)


@pytest.mark.asyncio
async def test_savepoint_rollback_releases_native_update_lock(test_engine):
    async with _committed_identity_scope_world(test_engine) as world:
        sessions = world["sessions"]
        job_id = world["project_job_id"]
        async with sessions() as holder:
            await holder.begin()
            savepoint = await holder.begin_nested()
            row = await holder.scalar(job_lock_statement(job_id, mode="update"))
            assert row is not None and row.id == job_id
            await savepoint.rollback()

            async with sessions() as contender:
                async with contender.begin():
                    row = await contender.scalar(
                        job_lock_statement(job_id, mode="share_nowait")
                    )
                    assert row is not None and row.id == job_id
            await holder.rollback()


@pytest.mark.asyncio
async def test_native_lock_query_preserves_identity_cache_unless_forced(test_engine):
    async with _committed_identity_scope_world(test_engine) as world:
        sessions = world["sessions"]
        job_id = world["project_job_id"]
        async with sessions() as reader:
            cached = await reader.scalar(select(AsyncJob).where(AsyncJob.id == job_id))
            assert cached is not None and cached.error_message is None
            await reader.commit()

            async with sessions() as writer:
                await writer.execute(
                    text(
                        "UPDATE public.async_jobs SET error_message=:value "
                        "WHERE id=:job_id"
                    ),
                    {"value": "fresh database value", "job_id": job_id},
                )
                await writer.commit()

            default_row = await reader.scalar(
                job_lock_statement(job_id, mode="share_nowait")
            )
            assert default_row is cached
            assert default_row.error_message is None

            refreshed_row = await reader.scalar(
                job_lock_statement(
                    job_id,
                    mode="share_nowait",
                    populate_existing=True,
                )
            )
            assert refreshed_row is cached
            assert refreshed_row.error_message == "fresh database value"
            await reader.rollback()


@pytest.mark.asyncio
async def test_lock_query_preserves_autoflush_and_read_only_transaction_rules(
    db_session, test_engine
):
    target = await async_job_service.create_job(db_session, kind="audit_archive")
    target_id = target.id

    default_pending = AsyncJob(kind="audit_archive", payload={"pending": "default"})
    db_session.add(default_pending)
    with _capture_sql(db_session) as statements:
        row = await db_session.scalar(
            job_lock_statement(target_id, mode="share_nowait")
        )
    assert row is not None and row.id == target_id
    normalized = [statement.lower() for statement in statements]
    insert_index = next(
        index
        for index, statement in enumerate(normalized)
        if re.search(r"\binsert\s+into\s+(?:public\.)?async_jobs\b", statement)
    )
    lock_index = next(
        index
        for index, statement in enumerate(normalized)
        if "aap_jobs_v1.read_job_for_share_nowait(" in statement
    )
    assert insert_index < lock_index

    no_autoflush_pending = AsyncJob(
        kind="audit_archive", payload={"pending": "suppressed"}
    )
    db_session.add(no_autoflush_pending)
    with _capture_sql(db_session) as statements:
        with db_session.no_autoflush:
            row = await db_session.scalar(
                job_lock_statement(target_id, mode="share_nowait")
            )
    assert row is not None and row.id == target_id
    assert inspect(no_autoflush_pending).pending
    assert not any(
        re.search(r"\binsert\s+into\s+(?:public\.)?async_jobs\b", statement.lower())
        for statement in statements
    )
    _assert_uses_only_lock_function(statements, "read_job_for_share_nowait")

    async with _committed_identity_scope_world(test_engine) as world:
        sessions = world["sessions"]
        job_id = world["project_job_id"]
        async with sessions() as read_only:
            await read_only.begin()
            await read_only.execute(text("SET TRANSACTION READ ONLY"))
            with pytest.raises(DBAPIError) as denied:
                await read_only.scalar(job_lock_statement(job_id, mode="update"))
            assert _sqlstate(denied.value) == "25006"
            await read_only.rollback()
