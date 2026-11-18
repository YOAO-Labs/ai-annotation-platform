"""PostgreSQL lease-clock case for the native Assets delivery functions."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from yoao_assets_source.import_delivery_contract import (
    DatasetImportDeliveryFailure,
    decode_dataset_import_delivery_claim,
    decode_dataset_import_delivery_completion,
)
from tests import test_dataset_import_native as native
from tests.test_dataset_import_native import import_world as import_world

RUN_NATIVE_CASE = os.environ.get("RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST") == "1"


@pytest.mark.skipif(
    not RUN_NATIVE_CASE,
    reason="run after migration 0214 is installed on a disposable PostgreSQL test DB",
)
async def test_delivery_lease_uses_wall_clock_inside_held_transactions(
    test_engine, import_world
):
    world = import_world
    connection_id = await native._connection(world)
    job_id = uuid4()
    lease_token = uuid4()
    next_token = uuid4()
    async with world.maker() as db:
        native._assert_ok(await native._accept(db, world, connection_id, job_id=job_id))
        await db.commit()
    async with world.maker() as db:
        token, _ = await native._claim(db, job_id)
        native._assert_ok(await native._manifest(db, job_id, token, []))
        await db.commit()
    async with world.maker() as db:
        finished = native._assert_ok(await native._finish(db, job_id, token))
        assert finished["stage"] == "completed"
        delivery_id = await db.scalar(
            text(
                "SELECT id FROM public.dataset_import_deliveries "
                "WHERE job_id = :job AND kind = 'notification'"
            ),
            {"job": job_id},
        )
        assert delivery_id is not None
        await db.commit()

    try:
        async with test_engine.connect() as connection:
            transaction = await connection.begin()
            null_lease = (
                await connection.execute(
                    text(
                        "SELECT yoao_assets_v1.claim_dataset_import_delivery("
                        ":lease_token, NULL)"
                    ),
                    {"lease_token": uuid4()},
                )
            ).scalar_one()
            with pytest.raises(DatasetImportDeliveryFailure, match="invalid_request"):
                decode_dataset_import_delivery_claim(null_lease)

            # The transaction clock starts before the sleep. Claiming afterwards
            # must grant a fresh lease based on wall clock, not that old timestamp.
            await connection.execute(text("SELECT pg_catalog.pg_sleep(31)"))
            claimed = (
                await connection.execute(
                    text(
                        "SELECT yoao_assets_v1.claim_dataset_import_delivery("
                        ":lease_token, 30)"
                    ),
                    {"lease_token": lease_token},
                )
            ).scalar_one()
            delivery = decode_dataset_import_delivery_claim(claimed)
            assert delivery is not None and delivery.delivery_id == delivery_id
            await transaction.commit()

        async with test_engine.begin() as connection:
            second_claim = (
                await connection.execute(
                    text(
                        "SELECT yoao_assets_v1.claim_dataset_import_delivery("
                        ":lease_token, 30)"
                    ),
                    {"lease_token": next_token},
                )
            ).scalar_one()
            assert decode_dataset_import_delivery_claim(second_claim) is None

        async with test_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE public.dataset_import_deliveries "
                    "SET lease_token = :lease_token, "
                    "lease_expires_at = pg_catalog.clock_timestamp() "
                    "+ interval '1 second' "
                    "WHERE id = :delivery_id"
                ),
                {"lease_token": lease_token, "delivery_id": delivery_id},
            )

        async with test_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SELECT pg_catalog.pg_sleep(2)"))
            expired = (
                await connection.execute(
                    text(
                        "SELECT yoao_assets_v1.complete_dataset_import_delivery("
                        ":delivery_id, :lease_token)"
                    ),
                    {"delivery_id": delivery_id, "lease_token": lease_token},
                )
            ).scalar_one()
            with pytest.raises(DatasetImportDeliveryFailure, match="lease_lost"):
                decode_dataset_import_delivery_completion(
                    expired,
                    delivery_id=delivery_id,
                    lease_token=lease_token,
                )
            await transaction.commit()
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM public.dataset_import_deliveries WHERE id = :id"),
                {"id": delivery_id},
            )
