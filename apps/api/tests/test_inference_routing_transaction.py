"""Routing admission must coexist with the caller's pending prediction references."""

import os
import uuid
from contextlib import AsyncExitStack

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models.prediction import Prediction
from app.services.ml_routing.authority import RoutingAuthority
from tests.factory import create_project, create_task, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _connect, _seed, _cleanup

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("connection_bound", [False, True])
async def test_admission_keeps_pending_prediction_and_outer_rollback(
    test_engine, monkeypatch, connection_bound
):
    await assert_owned_test_database(test_engine)
    for name in (
        "inference_routing_url",
        "inference_routing_database_url",
        "inference_routing_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")
    owner = await _connect(test_engine)
    world = await _seed(owner)
    before_receipts = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
        )
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    prediction_id = uuid.uuid4()
    try:
        async with AsyncExitStack() as stack:
            if connection_bound:
                connection = await stack.enter_async_context(test_engine.connect())
                db = await stack.enter_async_context(
                    AsyncSession(bind=connection, expire_on_commit=False)
                )
            else:
                db = await stack.enter_async_context(maker())
            actor = await create_user(
                db,
                "super_admin",
                f"route-tx-{uuid.uuid4()}@test.local",
                "Pending route caller",
            )
            project = await create_project(db, owner_id=actor.id)
            task = await create_task(db, project_id=project.id)
            db.add(
                Prediction(
                    id=prediction_id,
                    task_id=task.id,
                    project_id=project.id,
                    ml_backend_id=world["registry_id"],
                    ml_backend_pool_id=world["pool_id"],
                    result=[],
                )
            )
            await db.flush()
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM public.predictions WHERE id=$1", prediction_id
                )
                == 0
            )
            authority = RoutingAuthority(db.bind)
            scope = await authority.acquire(registry_id=world["registry_id"])
            assert scope.registry_id == world["registry_id"]
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM public.predictions WHERE id=$1", prediction_id
                )
                == 0
            )
            await authority.close(scope, "cancel")
            await db.rollback()
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM public.predictions WHERE id=$1", prediction_id
            )
            == 0
        )
    finally:
        try:
            after_receipts = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
                list(after_receipts - before_receipts),
            )
            await _cleanup(owner, world)
        finally:
            await owner.close()
