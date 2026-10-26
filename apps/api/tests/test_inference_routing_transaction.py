"""Routing admission must coexist with the caller's pending prediction references."""

import os
import socket
import uuid
from contextlib import AsyncExitStack

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime

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
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert routing_dsn, "module-data verifier must provide the restricted routing LOGIN"
    owner = await _connect(test_engine)
    world = await _seed(owner)
    owner_runtime = RoutingRuntime(routing_dsn)
    assert await owner_runtime.readiness()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    token = f"routing-owner-{uuid.uuid4().hex}"
    owner_server = PipelineHttpServer(
        manager=None,
        data=None,
        control_token=None,
        host="127.0.0.1",
        port=port,
        routing_manager=owner_runtime.manager,
        routing_control_token=token,
    )
    await owner_server.start()
    for name, value in {
        "inference_routing_url": f"http://127.0.0.1:{port}",
        "inference_routing_token": token,
        "inference_routing_token_file": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
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
            authority = RoutingAuthority()
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
            try:
                await owner_server.close()
            finally:
                try:
                    await owner_runtime.close()
                finally:
                    await owner.close()
