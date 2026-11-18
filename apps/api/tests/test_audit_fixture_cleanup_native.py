from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db.models.fixture_run import FixtureRun
from app.services import fixture_run_ownership as ownership
from yoao_audit.maintenance_contract import MAINTENANCE_PROTOCOL
from yoao_audit.maintenance_postgres import AuditMaintenancePostgresData


@pytest.fixture(autouse=True)
def isolated_fixture_settings(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)


async def _scope(db, namespace):
    from tests.seed_owner_support import OwnedRun

    root, run = uuid4(), uuid4()
    db.add(FixtureRun(id=root, family="owned"))
    await db.flush()
    await ownership.start_owned_scope(
        db, suite_id=root, run_id=run, namespace=namespace
    )
    return run, OwnedRun(root, run, namespace).scope().namespace


async def _event(db, namespace, *, identity=None, path="/api/v1/__test/seed/filtering"):
    if identity is None:
        statement = (
            "INSERT INTO public.audit_logs (action,path,detail_json) "
            "VALUES ('audit.fixture.cleanup_probe',:path,CAST(:detail AS jsonb)) "
            "RETURNING id,created_at"
        )
        params = {}
    else:
        statement = (
            "INSERT INTO public.audit_logs (id,created_at,action,path,detail_json) "
            "VALUES (:id,:created_at,'audit.fixture.cleanup_probe',:path,"
            "CAST(:detail AS jsonb)) RETURNING id,created_at"
        )
        params = {"id": identity[0], "created_at": identity[1]}
    params.update(path=path, detail=json.dumps({"namespace": namespace}))
    return tuple((await db.execute(text(statement), params)).one())


async def _visible(db, event_id):
    return set(
        (
            await db.execute(
                text(
                    "SELECT id,created_at FROM yoao_audit_v1.audit_events WHERE id=:id"
                ),
                {"id": event_id},
            )
        ).all()
    )


async def test_fixture_cleanup_preserves_composite_neighbors_and_rollback(db_session):
    data = AuditMaintenancePostgresData(db_session)
    await data.ensure_partition(datetime.now(timezone.utc).date().replace(day=1))
    namespace = uuid4().hex[:12]
    first, first_namespace = await _scope(db_session, namespace)
    second, second_namespace = await _scope(db_session, namespace)
    owned = await _event(db_session, first_namespace)
    neighbor = await _event(
        db_session,
        second_namespace,
        identity=(owned[0], owned[1] + timedelta(microseconds=1)),
    )
    ordinary = await _event(
        db_session,
        first_namespace,
        identity=(owned[0], owned[1] + timedelta(microseconds=2)),
    )
    await ownership.register_audit(db_session, first, *owned)
    await ownership.register_audit(db_session, second, *neighbor)
    assert await ownership.seal(db_session, first, family="owned") == ()

    await db_session.execute(text("SET LOCAL app.allow_audit_update=false"))
    with pytest.raises(DBAPIError) as denied:
        async with db_session.begin_nested():
            await data.cleanup_fixture_events(first)
    assert denied.value.orig.sqlstate == "42501"
    assert await _visible(db_session, owned[0]) == {owned, neighbor, ordinary}

    await db_session.execute(text("SET LOCAL app.allow_audit_update=true"))
    async with db_session.begin_nested() as rollback:
        assert await data.cleanup_fixture_events(first) == 1
        assert await _visible(db_session, owned[0]) == {neighbor, ordinary}
        await rollback.rollback()
    assert await _visible(db_session, owned[0]) == {owned, neighbor, ordinary}
    assert await data.cleanup_fixture_events(first) == 1
    assert await data.cleanup_fixture_events(first) == 0
    assert await _visible(db_session, owned[0]) == {neighbor, ordinary}


@pytest.mark.parametrize("blocker", ["open", "active", "unknown", "wrong_path"])
async def test_fixture_cleanup_refuses_unsettled_or_mismatched_receipts(
    db_session, blocker
):
    data = AuditMaintenancePostgresData(db_session)
    await data.ensure_partition(datetime.now(timezone.utc).date().replace(day=1))
    namespace = uuid4().hex[:12]
    run, rendered_namespace = await _scope(db_session, namespace)
    event = await _event(
        db_session,
        rendered_namespace,
        path="/ordinary"
        if blocker == "wrong_path"
        else "/api/v1/__test/seed/filtering",
    )
    await ownership.register_audit(db_session, run, *event)
    if blocker in {"active", "unknown"}:
        admission = await ownership.admit(db_session, run, "audit_probe")
        if blocker == "unknown":
            await db_session.execute(
                text(
                    "UPDATE yoao_fixture_v1.fixture_run_admissions SET state='unknown' WHERE id=:id"
                ),
                {"id": admission},
            )
    if blocker != "open":
        await ownership.seal(db_session, run, family="owned")
    await db_session.execute(text("SET LOCAL app.allow_audit_update=true"))
    with pytest.raises(DBAPIError) as denied:
        async with db_session.begin_nested():
            await data.cleanup_fixture_events(run)
    assert denied.value.orig.sqlstate == "55000"
    assert await _visible(db_session, event[0]) == {event}


async def test_fixture_cleanup_requires_authenticated_owner_not_definer(test_engine):
    suffix = uuid4().hex[:16]
    role = f"audit_fixture_probe_{suffix}"
    proxy = f"audit_cleanup_probe_{suffix}"
    password = secrets.token_urlsafe(32)
    created = False
    consumer = None
    try:
        async with test_engine.begin() as db:
            await db.execute(
                text(f"CREATE ROLE \"{role}\" LOGIN NOINHERIT PASSWORD '{password}'")
            )
            await db.execute(
                text(
                    f'GRANT CONNECT ON DATABASE "{test_engine.url.database}" TO "{role}"'
                )
            )
            await db.execute(
                text(f'GRANT USAGE ON SCHEMA yoao_audit_v1,yoao_fixture_v1 TO "{role}"')
            )
            await db.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION yoao_audit_v1.maintenance_command(jsonb) TO "{role}"'
                )
            )
            await db.execute(
                text(f"""
                CREATE FUNCTION yoao_fixture_v1.{proxy}(run_id uuid) RETURNS jsonb
                LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
                AS $function$ SELECT yoao_audit_v1.maintenance_command(jsonb_build_object(
                    'schema','aap-audit-maintenance/v1',
                    'operation','cleanup_fixture_events','run_id',run_id)) $function$
            """)
            )
            await db.execute(
                text(
                    f"REVOKE ALL ON FUNCTION yoao_fixture_v1.{proxy}(uuid) FROM PUBLIC"
                )
            )
            await db.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION yoao_fixture_v1.{proxy}(uuid) TO "{role}"'
                )
            )
        created = True
        consumer = create_async_engine(
            test_engine.url.set(username=role, password=password), hide_parameters=True
        )
        async with consumer.connect() as db:
            assert await db.scalar(text("SELECT SESSION_USER")) == role
            await db.execute(text("SET LOCAL app.allow_audit_update=true"))
            for statement, params in (
                (
                    "SELECT yoao_audit_v1.maintenance_command(CAST(:request AS jsonb))",
                    {
                        "request": json.dumps(
                            {
                                "schema": MAINTENANCE_PROTOCOL,
                                "operation": "cleanup_fixture_events",
                                "run_id": str(uuid4()),
                            }
                        )
                    },
                ),
                (
                    f"SELECT yoao_fixture_v1.{proxy}(CAST(:run AS uuid))",
                    {"run": uuid4()},
                ),
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(statement), params)
                assert denied.value.orig.sqlstate == "42501"
    finally:
        if consumer is not None:
            await consumer.dispose()
        if created:
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP FUNCTION yoao_fixture_v1.{proxy}(uuid)"))
                await db.execute(
                    text(
                        f'REVOKE ALL ON FUNCTION yoao_audit_v1.maintenance_command(jsonb) FROM "{role}"'
                    )
                )
                await db.execute(
                    text(
                        f'REVOKE ALL ON SCHEMA yoao_audit_v1,yoao_fixture_v1 FROM "{role}"'
                    )
                )
                await db.execute(
                    text(
                        f'REVOKE CONNECT ON DATABASE "{test_engine.url.database}" FROM "{role}"'
                    )
                )
                await db.execute(text(f'DROP ROLE "{role}"'))


@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires verifier-owned disposable PostgreSQL",
)
async def test_fixture_cleanup_refuses_a_non_test_database(test_engine):
    from tests.test_inference_backend_dependency_reads_restore import (
        _create_owned_clone,
        _drop_owned_clone,
    )

    database = "aap_audit_fixture_guard_" + uuid4().hex[:12]
    ownership_marker = "audit-fixture-guard:" + str(uuid4())
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    created = set()
    clone = None
    try:
        await _create_owned_clone(
            admin, test_engine, database, ownership_marker, [], created
        )
        clone = create_async_engine(
            test_engine.url.set(database=database), hide_parameters=True
        )
        async with clone.begin() as db:
            assert await db.scalar(text("SELECT current_database()")) == database
            await db.execute(text("CREATE SCHEMA aap_audit_v1"))
            await db.execute(text("CREATE SCHEMA yoao_fixture_v1"))
            await db.execute(
                text("CREATE TABLE yoao_fixture_v1.fixture_runs(id uuid PRIMARY KEY)")
            )
            source = (
                Path(__file__).resolve().parents[1]
                / "alembic/sql/0438_fixture_audit_namespace.sql"
            ).read_text()
            await db.exec_driver_sql(source)
            await db.execute(
                text(
                    "REVOKE ALL ON FUNCTION aap_audit_v1.maintenance_command(jsonb) FROM PUBLIC"
                )
            )
            await db.execute(text("SET LOCAL app.allow_audit_update=true"))
            request = json.dumps(
                {
                    "schema": MAINTENANCE_PROTOCOL,
                    "operation": "cleanup_fixture_events",
                    "run_id": str(uuid4()),
                }
            )
            with pytest.raises(DBAPIError) as denied:
                async with db.begin_nested():
                    await db.scalar(
                        text(
                            "SELECT aap_audit_v1.maintenance_command("
                            "CAST(:request AS jsonb))"
                        ),
                        {"request": request},
                    )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        if clone is not None:
            await clone.dispose()
        try:
            if database in created:
                await _drop_owned_clone(admin, test_engine, database, ownership_marker)
        finally:
            await admin.dispose()
