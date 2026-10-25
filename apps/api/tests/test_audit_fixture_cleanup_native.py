from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from aap_audit.maintenance_contract import MAINTENANCE_PROTOCOL
from aap_audit.maintenance_postgres import AuditMaintenancePostgresData


async def test_api_filtering_cleanup_propagates_audit_owner_failure(monkeypatch):
    from app.api.v1 import _test_seed

    outcomes = []

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _traceback):
            outcomes.append("rollback" if exc_type else "release")
            return False

    class Session:
        def begin_nested(self):
            return Savepoint()

    class MissingAuditOwner:
        def __init__(self, _session):
            pass

        async def cleanup_filtering_fixture_events(self, _namespace):
            raise RuntimeError("Audit owner command unavailable")

    monkeypatch.setattr(_test_seed, "AuditMaintenancePostgresData", MissingAuditOwner)
    with pytest.raises(HTTPException) as unavailable:
        await _test_seed._try_fixture_delete(
            Session(),
            _test_seed._AUDIT_FILTERING_FIXTURE_CLEANUP,
            {"namespace": "ab12cd34"},
        )
    assert unavailable.value.status_code == 503
    assert outcomes == ["rollback"]


async def test_filtering_fixture_cleanup_is_scoped_and_rollback_safe(db_session):
    data = AuditMaintenancePostgresData(db_session)
    month = datetime.now(timezone.utc).date().replace(day=1)
    await data.ensure_partition(month)

    namespace = uuid4().hex[:12]
    neighbor = uuid4().hex[:12]
    path = "/api/v1/__test/seed/filtering"
    rows = {
        "owned": {"namespace": namespace, "fixture": "filtering"},
        "neighbor": {"namespace": neighbor, "fixture": "filtering"},
        "shared": {"fixture": "filtering"},
        "wrong_path": {"namespace": namespace, "fixture": "filtering"},
    }
    ids = {}
    for name, detail in rows.items():
        row_path = path if name != "wrong_path" else "/api/v1/__test/seed/other"
        ids[name] = await db_session.scalar(
            text(
                "INSERT INTO public.audit_logs (action,path,detail_json) "
                "VALUES ('audit.fixture.cleanup_probe',:path,CAST(:detail AS jsonb)) "
                "RETURNING id"
            ),
            {"path": row_path, "detail": json.dumps(detail)},
        )

    async def visible_ids():
        return set(
            (
                await db_session.execute(
                    text(
                        "SELECT id FROM aap_audit_v1.audit_events "
                        "WHERE id=ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": list(ids.values())},
                )
            )
            .scalars()
            .all()
        )

    await db_session.execute(text('SET LOCAL "app.allow_audit_update" = false'))
    savepoint = await db_session.begin_nested()
    with pytest.raises(DBAPIError) as immutable_scope:
        await data.cleanup_filtering_fixture_events(namespace)
    assert immutable_scope.value.orig.sqlstate == "42501"
    await savepoint.rollback()

    savepoint = await db_session.begin_nested()
    with pytest.raises(DBAPIError) as malformed:
        await db_session.scalar(
            text("SELECT aap_audit_v1.maintenance_command(CAST(:request AS jsonb))"),
            {
                "request": json.dumps(
                    {
                        "schema": MAINTENANCE_PROTOCOL,
                        "operation": "cleanup_filtering_fixture_events",
                        "namespace": "Invalid-namespace",
                    }
                )
            },
        )
    assert malformed.value.orig.sqlstate == "22023"
    await savepoint.rollback()

    await db_session.execute(text('SET LOCAL "app.allow_audit_update" = true'))
    savepoint = await db_session.begin_nested()
    assert await data.cleanup_filtering_fixture_events(namespace) == 1
    assert await visible_ids() == {
        ids["neighbor"],
        ids["shared"],
        ids["wrong_path"],
    }
    await savepoint.rollback()
    assert await visible_ids() == set(ids.values())

    assert await data.cleanup_filtering_fixture_events(namespace) == 1
    assert await visible_ids() == {
        ids["neighbor"],
        ids["shared"],
        ids["wrong_path"],
    }

    shared_rows_before = await db_session.scalar(
        text(
            "SELECT count(*) FROM aap_audit_v1.audit_events "
            "WHERE detail_json ->> 'fixture' = 'filtering' "
            "AND path = '/api/v1/__test/seed/filtering'"
        )
    )
    savepoint = await db_session.begin_nested()
    assert await data.cleanup_filtering_fixture_events(None) == shared_rows_before
    assert await visible_ids() == {ids["wrong_path"]}
    await savepoint.rollback()
    assert await visible_ids() == {
        ids["neighbor"],
        ids["shared"],
        ids["wrong_path"],
    }


@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires verifier-owned disposable PostgreSQL",
)
async def test_filtering_fixture_cleanup_refuses_a_non_test_database(test_engine):
    from tests.test_inference_backend_dependency_reads_restore import (
        _create_owned_clone,
        _drop_owned_clone,
    )

    database = "aap_audit_fixture_guard_" + uuid4().hex[:12]
    ownership = "audit-fixture-guard:" + str(uuid4())
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    created = set()
    clone = None
    try:
        await _create_owned_clone(admin, test_engine, database, ownership, [], created)
        clone = create_async_engine(
            test_engine.url.set(database=database), hide_parameters=True
        )
        async with clone.begin() as db:
            assert await db.scalar(text("SELECT current_database()")) == database
            await db.execute(text("CREATE SCHEMA aap_audit_v1"))
            sql = (
                Path(__file__).resolve().parents[1]
                / "alembic/sql/0366_audit_fixture_cleanup.sql"
            ).read_text()
            await db.exec_driver_sql(sql)
            await db.execute(
                text(
                    "REVOKE ALL ON FUNCTION aap_audit_v1.maintenance_command(jsonb) FROM PUBLIC"
                )
            )
            await db.execute(text('SET LOCAL "app.allow_audit_update" = true'))
            with pytest.raises(DBAPIError) as denied:
                async with db.begin_nested():
                    await db.scalar(
                        text(
                            "SELECT aap_audit_v1.maintenance_command(CAST(:request AS jsonb))"
                        ),
                        {
                            "request": json.dumps(
                                {
                                    "schema": MAINTENANCE_PROTOCOL,
                                    "operation": "cleanup_filtering_fixture_events",
                                    "namespace": None,
                                }
                            )
                        },
                    )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        if clone is not None:
            await clone.dispose()
        try:
            if database in created:
                await _drop_owned_clone(admin, test_engine, database, ownership)
        finally:
            await admin.dispose()
