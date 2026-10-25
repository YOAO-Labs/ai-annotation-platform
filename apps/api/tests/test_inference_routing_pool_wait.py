"""Bound pool contention without changing the caller's transaction settings."""

import asyncio
import os
import time

import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect, _seed, _step

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_pool_wait_times_out_then_retries_with_original_transaction_settings(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    locker = await _connect(test_engine)
    client = await _connect(test_engine, role="inference_routing_client")
    lock_transaction = locker.transaction()
    try:
        await lock_transaction.start()
        await locker.fetchval(
            "SELECT id FROM public.ml_backend_service_pools WHERE id=$1 FOR NO KEY UPDATE",
            world["pool_id"],
        )
        async with client.transaction():
            await client.execute("SET LOCAL lock_timeout = '2s'")
            args = {
                "pool_id": str(world["pool_id"]),
                "registry_id": None,
                "project_id": None,
                "mode": "off",
                "required_instance_id": None,
                "health_max_age_seconds": 60,
            }
            began = time.monotonic()
            rejected = await asyncio.wait_for(
                _step(client, "admission_snapshot", args), timeout=1.5
            )
            assert time.monotonic() - began >= 0.1
            assert rejected["status"] == "rejected"
            assert rejected["reason"] == "management_busy"
            assert (
                await client.fetchval("SELECT current_setting('lock_timeout')") == "2s"
            )
            # The rejection rolls back its partial owner locks, preserving this
            # transaction so the same caller can retry after contention ends.
            await lock_transaction.rollback()
            accepted = await _step(client, "admission_snapshot", args)
            assert accepted["status"] == "ok"
            assert (
                await client.fetchval("SELECT current_setting('lock_timeout')") == "2s"
            )
    finally:
        await client.close()
        await locker.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()
