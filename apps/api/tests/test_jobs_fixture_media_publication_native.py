"""Jobs publication fences against a real owned fixture source and run."""

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from yoao_jobs.queue_contract import encode_dispatch_request

from app.services import fixture_run_ownership, image_pyramid_originals
from app.services.jobs_fixture_media import prepare_fixture_media_delivery
from tests.seed_owned_scenarios import _build_owned
from tests.seed_owner_support import private_owned_suite

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable fixture owner and managed original",
    ),
]


async def test_fixture_media_birth_and_seal_do_not_infer_publication_settlement(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with private_owned_suite() as suite:
        run = suite.new_run("jobspub" + uuid.uuid4().hex[:8])
        world = await _build_owned(suite.owner, run)
        async with sessions() as db:
            dataset_id = await db.scalar(
                text(
                    "SELECT item.dataset_id FROM aap_labeling_v1.task_rows AS task "
                    "JOIN public.dataset_items AS item ON item.id=task.dataset_item_id "
                    "WHERE task.id=:task"
                ),
                {"task": uuid.UUID(world["task_ids"][0])},
            )
        assert dataset_id is not None
        response = await suite.owner.post(
            "/api/v1/__test/seed/backfill-image",
            json={**run.payload(), "dataset_id": str(dataset_id)},
        )
        assert response.status_code == 200, response.text
        item_id = uuid.UUID(response.json()["item_id"])
        async with sessions() as db:
            try:
                original = await image_pyramid_originals.pin(db, "dataset_item", item_id)
                source = image_pyramid_originals.receipt(original)
                delegation = image_pyramid_originals.system_authorization(
                    "dataset_item", item_id, original
                )
                queue = f"aap_workspace_worker_{run.run_id.hex}_pyramid"

                async def prepare(delivery_id, *, target=item_id):
                    envelope = json.loads(
                        encode_dispatch_request(
                            "app.workers.image_pyramid.generate_image_pyramid",
                            str(delivery_id),
                            ["dataset_item", str(target), False, delegation],
                            options={"queue": queue},
                        )
                    )
                    return await prepare_fixture_media_delivery(
                        db,
                        run_id=run.run_id,
                        dataset_id=dataset_id,
                        item_id=item_id,
                        owner_kind="dataset_item",
                        owner_id=item_id,
                        producer_kind="pyramid",
                        delivery_id=delivery_id,
                        queue=queue,
                        source=source,
                        envelope=envelope,
                    )

                first_id, second_id = uuid.uuid4(), uuid.uuid4()
                first = await prepare(first_id)
                second = await prepare(second_id)
                assert await prepare(first_id) == first
                with pytest.raises(DBAPIError):
                    async with db.begin_nested():
                        await prepare(first_id, target=uuid.uuid4())
                await db.execute(
                    text(
                        "SELECT pg_catalog.pg_advisory_xact_lock("
                        "pg_catalog.hashtextextended('aap-jobs:fixture-media:' || "
                        "CAST(CAST(:delivery AS uuid) AS text), 0))"
                    ),
                    {"delivery": first_id},
                )
                entered = await db.scalar(
                    text("SELECT aap_jobs_v1.begin_fixture_media_publication(:delivery,:nonce)"),
                    {"delivery": first_id, "nonce": uuid.uuid4()},
                )
                assert entered["state"] == "entered"
                assert not await db.scalar(
                    text(
                        "SELECT aap_jobs_v1.fixture_media_publication_accepted("
                        ":run,:delivery,:digest)"
                    ),
                    {"run": run.run_id, "delivery": first_id, "digest": first["payload_digest"]},
                )
                await fixture_run_ownership.seal(db, run.run_id, family="owned")
                for delivery_id, receipt in ((first_id, first), (second_id, second)):
                    assert await db.scalar(
                        text(
                            "SELECT aap_jobs_v1.retire_fixture_media_publication("
                            ":run,:delivery,:digest)"
                        ),
                        {"run": run.run_id, "delivery": delivery_id, "digest": receipt["payload_digest"]},
                    ) == "blocked"
                    assert not await db.scalar(
                        text(
                            "SELECT aap_jobs_v1.fixture_media_publication_retired("
                            ":run,:delivery,:digest)"
                        ),
                        {"run": run.run_id, "delivery": delivery_id, "digest": receipt["payload_digest"]},
                    )
                with pytest.raises(DBAPIError):
                    async with db.begin_nested():
                        await prepare(uuid.uuid4())
            finally:
                await db.rollback()
        async with sessions() as db:
            for delivery_id in (first_id, second_id):
                assert await db.scalar(
                    text("SELECT aap_jobs_v1.fixture_media_delivery(:delivery)"),
                    {"delivery": delivery_id},
                ) is None
