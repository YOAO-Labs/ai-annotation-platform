"""Saved function-only proof for the fixed Inference Pool reference read."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_owner_data.postgres import verify_function_only_role
from app.db.models.ml_backend_pool import MLBackendServicePool
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_ROLE = "inference_pool_reference_client"
_FUNCTIONS = {("ai_mask_pool_reference_row", "uuid")}


async def test_pool_reference_reads_empty_pool_in_caller_transaction_and_hides_table(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    pool_id = uuid.uuid4()
    async with maker.begin() as db:
        db.add(
            MLBackendServicePool(
                id=pool_id,
                name=f"empty lineage pool {uuid.uuid4().hex[:10]}",
                enabled=False,
                legacy_instance_id=None,
            )
        )

    connection = None
    transaction = None
    try:
        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_INFERENCE_POOL_REFERENCE_URL",
                _ROLE,
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_inference_v1",
            functions=_FUNCTIONS,
            role=_ROLE,
        )
        transaction = connection.transaction()
        await transaction.start()
        transaction_id = await connection.fetchval(
            "SELECT pg_catalog.pg_current_xact_id()::text"
        )
        row = await connection.fetchrow(
            "SELECT * FROM aap_inference_v1.ai_mask_pool_reference_row($1::uuid)",
            pool_id,
        )
        assert row is not None
        assert tuple(row.keys()) == (
            "id",
            "name",
            "enabled",
            "routing_policy",
            "legacy_instance_id",
            "routing_generation",
            "capability_fingerprint",
            "capability_snapshot",
            "created_at",
            "updated_at",
        )
        assert row["id"] == pool_id
        assert row["legacy_instance_id"] is None
        assert (
            await connection.fetchrow(
                "SELECT * FROM aap_inference_v1.ai_mask_pool_reference_row($1::uuid)",
                uuid.uuid4(),
            )
            is None
        )
        assert (
            await connection.fetchval("SELECT pg_catalog.pg_current_xact_id()::text")
            == transaction_id
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT id FROM public.ml_backend_service_pools LIMIT 1"
            )
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            await db.execute(
                delete(MLBackendServicePool).where(MLBackendServicePool.id == pool_id)
            )
