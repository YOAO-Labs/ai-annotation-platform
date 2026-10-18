"""Migration refusal preserves accepted connector-import work."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from tests import test_dataset_import_native as native
from tests.test_dataset_import_native import import_world as import_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]


def _invoke(sync_connection, direction):
    path = (
        Path(__file__).resolve().parents[1] / "alembic/versions/0214_dataset_import.py"
    )
    spec = importlib.util.spec_from_file_location("import_migration_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_connection)):
        getattr(module, direction)()


async def _assert_contract_still_installed(connection):
    assert await connection.scalar(
        text("SELECT to_regclass('public.dataset_import_operations') IS NOT NULL")
    )
    assert await connection.scalar(
        text(
            "SELECT to_regprocedure('aap_assets_v1.claim_dataset_import(uuid,integer,uuid)') IS NOT NULL"
        )
    )


async def test_upgrade_refuses_active_legacy_job_before_any_ddl(test_engine):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        job = uuid4()
        await connection.execute(
            text(
                "INSERT INTO public.async_jobs(id,kind,status,payload,result) "
                "VALUES (:id,'dataset_import','pending','{}'::jsonb,'{}'::jsonb)"
            ),
            {"id": job},
        )
        with pytest.raises(RuntimeError, match="legacy dataset_import"):
            await connection.run_sync(_invoke, "upgrade")
        await _assert_contract_still_installed(connection)
        assert (
            await connection.scalar(
                text("SELECT status FROM public.async_jobs WHERE id=:id"), {"id": job}
            )
            == "pending"
        )
        await transaction.rollback()


async def test_downgrade_refuses_accepted_active_operation(test_engine, import_world):
    connection_id = await native._connection(import_world)
    async with import_world.maker.begin() as db:
        reply = await native._accept(db, import_world, connection_id)
        assert reply["ok"] is True, reply
    async with test_engine.connect() as connection:
        with pytest.raises(RuntimeError, match="active"):
            await connection.run_sync(_invoke, "downgrade")
        await _assert_contract_still_installed(connection)
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_import_operations WHERE dataset_id=:id"
                ),
                {"id": import_world.dataset_id},
            )
            == 1
        )
        await connection.rollback()


async def test_downgrade_refuses_undelivered_followup(test_engine):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text(
                "INSERT INTO public.dataset_import_deliveries(id,job_id,kind,target_id) "
                "VALUES (:id,:job,'notification',:target)"
            ),
            {"id": uuid4(), "job": uuid4(), "target": uuid4()},
        )
        with pytest.raises(RuntimeError, match="pending delivery"):
            await connection.run_sync(_invoke, "downgrade")
        await _assert_contract_still_installed(connection)
        await transaction.rollback()
