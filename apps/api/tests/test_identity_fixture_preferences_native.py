"""Permission, lock and physical-database guards for fixture preference writes."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
import subprocess
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from scripts import restore_screenshot_user_preferences as preferences
from tests.identity_recording_fixture_helpers import (
    ROOT,
    recording_graph,
    recording_snapshot,
    recording_owner_session,
    recording_consumer as recording_consumer,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


async def test_fixture_preferences_node_and_borrowed_rollback(
    test_engine, recording_consumer, record_property
):
    async with recording_graph(test_engine) as ids:
        before = await recording_snapshot(test_engine, ids)
        url, consumer = recording_consumer
        result = await asyncio.to_thread(
            subprocess.run,
            ["node", str(ROOT / "apps/api/tests/fixtures/identity_recording_peer.mjs")],
            input=json.dumps(
                {
                    "database_url": url.set(drivername="postgresql").render_as_string(
                        hide_password=False
                    ),
                    "user_id": str(ids["recorder"]),
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "ok": True,
            "private_user_denied": True,
            "rolled_back": True,
            "updated": [1, 1, 0, 0],
        }
        assert await recording_snapshot(test_engine, ids) == before
        async with consumer.connect() as db:
            transaction = await db.begin()
            assert (
                await db.scalar(
                    text(
                        "SELECT yoao_identity_v1.replace_fixture_preferences(:id,CAST(:preferences AS jsonb))"
                    ),
                    {
                        "id": ids["recorder"],
                        "preferences": json.dumps({"python": True}),
                    },
                )
                == 1
            )
            await transaction.rollback()
        assert await recording_snapshot(test_engine, ids) == before
        async with test_engine.connect() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege('identity_api_consumer','yoao_identity_v1.replace_fixture_preferences(uuid,jsonb)','EXECUTE')"
                )
            )
        record_property("fixture_preferences_node", json.loads(result.stdout))


async def test_fixture_preferences_waits_for_the_preferences_row(
    test_engine, tmp_path, record_property
):
    async with recording_graph(test_engine) as ids:
        async with recording_owner_session() as owner:
            role = await owner.scalar(text("SELECT current_user"))
        path = tmp_path / "preferences.json"
        path.write_text('{"after_lock":"new"}')
        pending = None
        async with test_engine.connect() as locker:
            transaction = await locker.begin()
            await locker.execute(
                text(
                    "SELECT user_id FROM public.identity_account_preferences WHERE user_id=:id FOR UPDATE"
                ),
                {"id": ids["recorder"]},
            )
            try:
                pending = asyncio.create_task(
                    preferences.restore(ids["recorder"], path)
                )
                deadline = asyncio.get_running_loop().time() + 15
                waiting = None
                while asyncio.get_running_loop().time() < deadline:
                    assert not pending.done(), (
                        pending.result() if pending.done() else None
                    )
                    async with test_engine.connect() as db:
                        waiting = (
                            (
                                await db.execute(
                                    text(
                                        "SELECT pid,wait_event_type,wait_event FROM pg_stat_activity WHERE datname=current_database() AND usename=:role AND state='active' AND wait_event_type='Lock' AND position('replace_fixture_preferences' in query)>0"
                                    ),
                                    {"role": role},
                                )
                            )
                            .mappings()
                            .first()
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.03)
                assert waiting is not None
                record_property("fixture_preferences_row_lock", dict(waiting))
            finally:
                await transaction.rollback()
                if pending is not None:
                    await asyncio.wait_for(asyncio.shield(pending), 15)
        async with test_engine.connect() as db:
            assert await db.scalar(
                text(
                    "SELECT preferences FROM public.identity_account_preferences WHERE user_id=:id"
                ),
                {"id": ids["recorder"]},
            ) == {"after_lock": "new"}


@asynccontextmanager
async def isolated_preference_database(test_engine, suffix):
    _verify_postgres(test_engine.url)
    assert suffix in ("_e2e", "_dev", "_TEST")
    name = "aap_fixture_pref_" + uuid.uuid4().hex[:12] + suffix
    marker = "fixture-pref:" + uuid.uuid4().hex
    signature = "yoao_identity_v1.replace_fixture_preferences(uuid,jsonb)"
    engine = None
    async with test_engine.connect() as raw:
        db = await raw.execution_options(isolation_level="AUTOCOMMIT")
        definition = await db.scalar(
            text(
                "SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"
            ),
            {"signature": signature},
        )
        await db.execute(text(f'CREATE DATABASE "{name}"'))
        await db.execute(text(f"""COMMENT ON DATABASE "{name}" IS '{marker}'"""))
        oid = await db.scalar(
            text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": name}
        )
    try:
        engine = create_async_engine(
            test_engine.url.set(database=name), hide_parameters=True
        )
        async with engine.begin() as db:
            await db.execute(text("CREATE SCHEMA yoao_identity_v1"))
            await db.execute(
                text(
                    "CREATE TABLE public.identity_account_preferences("
                    "user_id uuid PRIMARY KEY, preferences jsonb NOT NULL DEFAULT '{}'::jsonb)"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO public.identity_account_preferences(user_id) "
                    "VALUES ('10000000-0000-0000-0000-000000000001')"
                )
            )
            await db.exec_driver_sql(definition)
            await db.execute(text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))
            await db.execute(
                text("GRANT USAGE ON SCHEMA yoao_identity_v1,public TO identity_code")
            )
            await db.execute(
                text(
                    "GRANT SELECT(user_id), UPDATE(preferences) "
                    "ON public.identity_account_preferences TO identity_code"
                )
            )
            await db.execute(text(f"ALTER FUNCTION {signature} OWNER TO identity_code"))
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        async with test_engine.connect() as raw:
            db = await raw.execution_options(isolation_level="AUTOCOMMIT")
            assert (
                await db.execute(
                    text(
                        "SELECT oid,shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                    ),
                    {"name": name},
                )
            ).one() == (oid, marker)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                    ),
                    {"name": name},
                )
                == 0
            )
            await db.execute(text(f'DROP DATABASE "{name}"'))


@pytest.mark.parametrize("suffix", ["_e2e", "_dev", "_TEST"])
async def test_fixture_preferences_uses_actual_case_sensitive_database_name(
    test_engine, suffix, record_property
):
    async with isolated_preference_database(test_engine, suffix) as engine:
        command = text(
            "SELECT yoao_identity_v1.replace_fixture_preferences('10000000-0000-0000-0000-000000000001'::uuid,CAST(:preferences AS jsonb))"
        ).bindparams(preferences=json.dumps({"scope": True}))
        if suffix == "_e2e":
            async with engine.begin() as db:
                assert await db.scalar(command) == 1
        else:
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as db:
                    await db.scalar(command)
            assert error.value.orig.sqlstate == "42501"
        async with engine.connect() as db:
            stored = await db.scalar(
                text("SELECT preferences FROM public.identity_account_preferences")
            )
            assert stored == ({"scope": True} if suffix == "_e2e" else {})
    record_property(
        "fixture_preferences_database_guard", {"suffix": suffix, "removed": True}
    )
