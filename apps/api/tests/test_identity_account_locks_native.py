"""Native and execute-only coverage for same-transaction account locks."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aap_identity
from aap_identity.account_lock_contract import (
    ACCOUNT_LOCK_PROTOCOL,
    AccountLockRequest,
    decode_account_lock_reply,
)
from tests.test_identity_auth_native import _cleanup, _seed

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SQL = _SQL_DIR / "0251_identity_account_locks.sql"
_SCHEMA = _SQL_DIR / "0251_identity_account_locks.schema.json"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0251_identity_account_locks.py"
)
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/account_lock_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "account_lock_protocol.schema.json"
    )

_FUNCTIONS = {
    "share": "aap_identity_v1.lock_accounts_share",
    "update": "aap_identity_v1.lock_accounts_update",
    "key_share": "aap_identity_v1.lock_accounts_key_share",
}
_SIGNATURES = {
    mode: f"{function}(uuid[],boolean)" for mode, function in _FUNCTIONS.items()
}


def _json_object(value):
    return json.loads(value) if isinstance(value, str) else value


def _sqlstate(error: BaseException) -> str | None:
    return getattr(error, "sqlstate", None) or getattr(
        getattr(error, "orig", None), "sqlstate", None
    )


async def _call(session, mode: str, account_ids, *, nowait: bool):
    statement = f"SELECT {_FUNCTIONS[mode]}(CAST(:account_ids AS uuid[]), :nowait)"
    return (
        await session.execute(
            text(statement), {"account_ids": account_ids, "nowait": nowait}
        )
    ).scalar_one()


def _request(account_ids, mode: str, *, nowait: bool) -> AccountLockRequest:
    return AccountLockRequest(
        schema=ACCOUNT_LOCK_PROTOCOL,
        account_ids=account_ids,
        mode=mode,
        nowait=nowait,
    )


def test_account_lock_artifact_matches_the_portable_contract():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    sql = _SQL.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")

    assert schema["$id"] == ACCOUNT_LOCK_PROTOCOL
    assert schema["$defs"] == canonical["$defs"]
    assert schema["x-postgresql-functions"] == [
        "aap_identity_v1.lock_accounts_key_share(uuid[], boolean)",
        "aap_identity_v1.lock_accounts_share(uuid[], boolean)",
        "aap_identity_v1.lock_accounts_update(uuid[], boolean)",
    ]
    assert "SQLSTATE 22023" in schema["$comment"]
    assert "55P03, 40001, and 40P01" in schema["$comment"]
    assert 'revision = "0251"' in migration
    assert 'down_revision = "0250"' in migration
    assert sql.count("CREATE FUNCTION aap_identity_v1.lock_accounts_") == 3
    assert "RETURNS jsonb" in sql
    assert "VOLATILE" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "FOR SHARE OF account" in sql
    assert "FOR UPDATE OF account" in sql
    assert "FOR KEY SHARE OF account" in sql
    assert "ORDER BY account.id" in sql
    assert "ARRAY[]" not in sql
    assert "EXCEPTION WHEN" not in sql.upper()
    assert "INSERT INTO" not in sql.upper()
    assert "UPDATE PUBLIC." not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
    assert "COMMIT;" not in sql.upper()
    assert "ROLLBACK;" not in sql.upper()


@pytest.mark.asyncio
async def test_locks_return_sorted_unique_unfiltered_current_account_facts(test_engine):
    first = await _seed(
        test_engine, active=False, role="historic_reviewer", create_key=False
    )
    second = await _seed(test_engine, active=True, role="viewer", create_key=False)
    requested_ids = [second["actor_id"], first["actor_id"], second["actor_id"]]
    missing_id = uuid.uuid4()
    requested_ids.insert(1, missing_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET name=:name WHERE id=:id"),
                {"name": "Historic account row", "id": first["actor_id"]},
            )
            await session.execute(
                text("UPDATE public.users SET name=:name WHERE id=:id"),
                {"name": "Active account row", "id": second["actor_id"]},
            )

        request = _request(requested_ids, "share", nowait=True)
        async with maker.begin() as session:
            raw = _json_object(
                await _call(session, "share", requested_ids, nowait=True)
            )
            facts = decode_account_lock_reply(raw, request)

        assert [fact.id for fact in facts] == sorted(
            [first["actor_id"], second["actor_id"]], key=str
        )
        by_id = {fact.id: fact for fact in facts}
        assert by_id[first["actor_id"]].name == "Historic account row"
        assert by_id[first["actor_id"]].role == "historic_reviewer"
        assert by_id[first["actor_id"]].is_active is False
        assert by_id[second["actor_id"]].name == "Active account row"
        assert by_id[second["actor_id"]].role == "viewer"
        assert by_id[second["actor_id"]].is_active is True

        empty_request = _request([], "update", nowait=False)
        async with maker.begin() as session:
            empty = _json_object(await _call(session, "update", [], nowait=False))
        assert decode_account_lock_reply(empty, empty_request) == []
    finally:
        await _cleanup(test_engine, first)
        await _cleanup(test_engine, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["share", "update", "key_share"])
async def test_each_mode_holds_its_postgres_row_lock_strength(test_engine, mode):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as holder:
            async with holder.begin():
                raw = _json_object(
                    await _call(holder, mode, [fixture["actor_id"]], nowait=True)
                )
                assert decode_account_lock_reply(
                    raw,
                    _request([fixture["actor_id"]], mode, nowait=True),
                )

                async with maker() as contender:
                    if mode == "key_share":
                        row = await contender.scalar(
                            text(
                                "SELECT id FROM public.users WHERE id=:id "
                                "FOR NO KEY UPDATE NOWAIT"
                            ),
                            {"id": fixture["actor_id"]},
                        )
                        assert row == fixture["actor_id"]
                    else:
                        with pytest.raises(DBAPIError) as captured:
                            await contender.execute(
                                text(
                                    "SELECT id FROM public.users WHERE id=:id "
                                    "FOR NO KEY UPDATE NOWAIT"
                                ),
                                {"id": fixture["actor_id"]},
                            )
                        assert _sqlstate(captured.value) == "55P03"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["share", "update", "key_share"])
async def test_nowait_false_waits_and_returns_the_locked_row_version(test_engine, mode):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    waiter_started = asyncio.Event()
    wait_task = None
    holder = maker()
    try:
        await holder.begin()
        await holder.execute(
            text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
            {"id": fixture["actor_id"]},
        )
        await holder.execute(
            text("UPDATE public.users SET name=:name WHERE id=:id"),
            {"name": "Committed while waiter waits", "id": fixture["actor_id"]},
        )

        async def wait_for_account():
            async with maker.begin() as session:
                waiter_started.set()
                request = _request([fixture["actor_id"]], mode, nowait=False)
                raw = _json_object(
                    await _call(session, mode, [fixture["actor_id"]], nowait=False)
                )
                return decode_account_lock_reply(raw, request)

        wait_task = asyncio.create_task(wait_for_account())
        await waiter_started.wait()
        await asyncio.sleep(0.05)
        assert not wait_task.done()
        await holder.commit()
        facts = await asyncio.wait_for(wait_task, timeout=5)
        assert len(facts) == 1
        assert facts[0].id == fixture["actor_id"]
        assert facts[0].name == "Committed while waiter waits"
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["share", "update", "key_share"])
async def test_nowait_lock_conflict_propagates_and_aborts_the_transaction(
    test_engine, mode
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    dsn = test_engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    connection = None
    transaction = None
    try:
        async with maker() as holder:
            async with holder.begin():
                await holder.execute(
                    text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                    {"id": fixture["actor_id"]},
                )
                connection = await asyncpg.connect(dsn)
                transaction = connection.transaction()
                await transaction.start()
                with pytest.raises(asyncpg.LockNotAvailableError) as captured:
                    await connection.fetchval(
                        f"SELECT {_FUNCTIONS[mode]}($1::uuid[], TRUE)",
                        [fixture["actor_id"]],
                    )
                assert captured.value.sqlstate == "55P03"
                with pytest.raises(asyncpg.InFailedSQLTransactionError) as aborted:
                    await connection.fetchval("SELECT 1")
                assert aborted.value.sqlstate == "25P02"
                await transaction.rollback()
                transaction = None
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["share", "update", "key_share"])
@pytest.mark.parametrize(
    ("account_ids", "nowait"),
    [(None, True), ([uuid.uuid4(), None], True), ([], None)],
)
async def test_invalid_lock_arguments_raise_invalid_request_sqlstate(
    test_engine, mode, account_ids, nowait
):
    dsn = test_engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    connection = await asyncpg.connect(dsn)
    try:
        with pytest.raises(Exception) as captured:
            await connection.fetchval(
                f"SELECT {_FUNCTIONS[mode]}($1::uuid[], $2::boolean)",
                account_ids,
                nowait,
            )
        assert captured.value.sqlstate == "22023"
        assert "invalid_request" in str(captured.value)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_account_lock_execute_grants_are_limited_to_required_callers(test_engine):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity module-owner grants require the module-data verifier")
    expected = {
        "workspace_code": {"share", "update", "key_share"},
        "labeling_code": {"share"},
        "inference_code": {"update"},
        "learning_code": {"share"},
        "append_client": {"share", "update", "key_share"},
        "identity_auth_client": set(),
        "identity_command_client": set(),
        "identity_fence_client": set(),
        "identity_session_operator": set(),
    }
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        for role, allowed_modes in expected.items():
            for mode, signature in _SIGNATURES.items():
                permitted = await session.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege("
                        "CAST(:role AS name), CAST(:signature AS text), 'EXECUTE')"
                    ),
                    {"role": role, "signature": signature},
                )
                assert permitted is (mode in allowed_modes), (role, mode)
        assert await session.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege("
                "'append_client', 'aap_identity_v1.read_actor_facts(uuid)', "
                "'EXECUTE')"
            )
        )


@pytest.mark.asyncio
async def test_workflow_login_has_only_account_function_capabilities_and_no_user_table(
    test_engine,
):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("the execute-only workflow LOGIN requires the module-data verifier")
    url = os.environ.get("AAP_MODULE_DATA_APPEND_URL")
    test_url = os.environ.get("TEST_DATABASE_URL")
    assert url and test_url
    parsed, expected = make_url(url), make_url(test_url)
    assert (
        parsed.drivername == "postgresql+asyncpg"
        and parsed.username == "append_client"
        and parsed.host == "127.0.0.1"
        and parsed.port == expected.port
        and parsed.database == expected.database
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_user")) == "append_client"
            )
            for signature in [
                *_SIGNATURES.values(),
                "aap_identity_v1.read_actor_facts(uuid)",
            ]:
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        ":signature, 'EXECUTE')"
                    ),
                    {"signature": signature},
                )
            assert not await connection.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user, "
                    "'public.users', 'SELECT')"
                )
            )
            assert not await connection.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user, "
                    "'public.users', 'SELECT')"
                )
            )
            assert _json_object(
                await connection.scalar(
                    text(
                        "SELECT aap_identity_v1.lock_accounts_share("
                        "ARRAY[]::uuid[], TRUE)"
                    )
                )
            ) == {"schema": ACCOUNT_LOCK_PROTOCOL, "accounts": []}
            assert (
                await connection.scalar(
                    text("SELECT actor_id FROM aap_identity_v1.read_actor_facts(:id)"),
                    {"id": uuid.uuid4()},
                )
                is None
            )
            with pytest.raises(DBAPIError) as captured:
                await connection.execute(text("SELECT id FROM public.users LIMIT 1"))
            assert _sqlstate(captured.value) == "42501"
    finally:
        await engine.dispose()
