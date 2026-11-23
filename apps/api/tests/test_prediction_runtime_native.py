"""Prediction transactions on the ordinary LOGIN of a disposable verifier database."""

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.storage_usage import StorageUsageMutation
from app.services import prediction_inputs
from app.services.inference_topology_data import InferenceTopologyData
from app.services.prediction_execution import (
    create_snapshot,
    load_snapshot,
    prune_retained_snapshots,
)
from tests.conftest import create_registry_with_pool
from tests.test_prediction_inputs import input_storage_world
from tests.test_workspace_project_authority_locking import _api_url, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and the real application LOGIN",
    ),
]


async def _assert_denied(db, statement):
    with pytest.raises(DBAPIError) as denied:
        async with db.begin_nested():
            await db.execute(text(statement))
    assert denied.value.orig.sqlstate == "42501"


async def test_prediction_locks_and_snapshots_borrow_the_ordinary_transaction(
    test_engine,
):
    async with _world(test_engine) as (owner, api, world, url):
        capabilities = {"models": [{"id": "video", "task": "tracker"}]}
        project_id = world["project_id"]
        async with owner.begin() as db:
            backend, pool = await create_registry_with_pool(
                db,
                name=f"prediction-native-{uuid.uuid4().hex}",
                enabled_pool=True,
                auth_method="bearer",
                auth_token="registry-token-not-a-projection",
                health_meta={"capabilities": capabilities, "probe_note": "private"},
            )
            registry_id, pool_id = backend.id, pool.id
            db.add(
                ProjectMLBackendPool(
                    project_id=project_id, pool_id=pool_id, enabled=True
                )
            )
        try:
            async with api() as db:
                assert await db.scalar(text("SELECT session_user")) == url.username
                topology = InferenceTopologyData(db)
                facts = await topology.lock_video_prediction_registry(registry_id)
                assert dict(facts._mapping) == {
                    "state": "connected",
                    "capabilities": capabilities,
                }
                assert (
                    await topology.lock_video_prediction_pool(
                        project_id, registry_id, pool_id
                    )
                    == pool_id
                )
                assert await topology.lock_native_prediction_route(
                    project_id, pool_id, registry_id
                )
                for model, where in (
                    (MLBackendRegistry, MLBackendRegistry.id == registry_id),
                    (MLBackendServicePool, MLBackendServicePool.id == pool_id),
                    (
                        MLBackendPoolMember,
                        MLBackendPoolMember.registry_id == registry_id,
                    ),
                    (ProjectMLBackendPool, ProjectMLBackendPool.pool_id == pool_id),
                ):
                    async with owner() as writer:
                        with pytest.raises(DBAPIError) as busy:
                            await writer.execute(
                                select(model.id)
                                .where(where)
                                .with_for_update(nowait=True)
                            )
                        assert busy.value.orig.sqlstate == "55P03"
                payload = {"stages": [], "execution_scope": "bulk"}
                snapshot = await create_snapshot(db, project_id, payload)
                assert await load_snapshot(db, snapshot.id, project_id) == (
                    payload,
                    snapshot.digest,
                )
                with pytest.raises(ValueError, match="快照"):
                    await load_snapshot(db, snapshot.id, uuid.uuid4())
                await prune_retained_snapshots(
                    db, datetime.now(timezone.utc) + timedelta(seconds=1)
                )
                with pytest.raises(ValueError, match="快照"):
                    await load_snapshot(db, snapshot.id, project_id)
                for relation in (
                    "ml_backend_registry",
                    "ml_backend_service_pools",
                    "ml_backend_pool_members",
                    "project_ml_backend_pool",
                    "project_pipelines",
                    "prediction_execution_snapshots",
                    "prediction_input_leases",
                    "prediction_retry_deliveries",
                ):
                    column = (
                        "job_id" if relation == "prediction_retry_deliveries" else "id"
                    )
                    await _assert_denied(
                        db, f"SELECT {column} FROM public.{relation} LIMIT 1"
                    )
                for schema in ("yoao_jobs_v1", "yoao_labeling_v1"):
                    await _assert_denied(
                        db,
                        f"SELECT {schema}.prediction_execution_snapshot_in_use(NULL::uuid)",
                    )
                rolled_back = await create_snapshot(db, project_id, payload)
                await db.rollback()
            async with owner() as db:
                assert await db.get(PredictionExecutionSnapshot, rolled_back.id) is None
                await db.execute(
                    select(MLBackendRegistry.id)
                    .where(MLBackendRegistry.id == registry_id)
                    .with_for_update(nowait=True)
                )
                async with api() as reader:
                    with pytest.raises(DBAPIError) as busy:
                        await InferenceTopologyData(
                            reader
                        ).lock_video_prediction_registry(registry_id)
                    assert busy.value.orig.sqlstate == "55P03"
        finally:
            async with owner.begin() as db:
                for model, where in (
                    (ProjectMLBackendPool, ProjectMLBackendPool.pool_id == pool_id),
                    (MLBackendPoolMember, MLBackendPoolMember.pool_id == pool_id),
                    (MLBackendServicePool, MLBackendServicePool.id == pool_id),
                    (MLBackendRegistry, MLBackendRegistry.id == registry_id),
                ):
                    await db.execute(delete(model).where(where))


async def test_prediction_input_publication_and_reclaim_as_ordinary_login(
    test_engine, monkeypatch
):
    url = _api_url(test_engine)
    engine = create_async_engine(url, hide_parameters=True)
    api = async_sessionmaker(engine, expire_on_commit=False)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with input_storage_world(test_engine, monkeypatch) as storage:
            async with api() as db:
                assert await db.scalar(text("SELECT session_user")) == url.username
                await _assert_denied(
                    db, "SELECT inventory FROM public.prediction_input_leases"
                )
            async with prediction_inputs.prediction_inputs(
                api,
                owner_id=storage.owner_id,
                owner_kind="batch_predict",
                task_id=storage.task_id,
            ) as inputs:
                identifier = inputs.record.id
                task = SimpleNamespace(id=storage.task_id)
                signed = await inputs.upload_crop(
                    task, 0, b"a version-pinned prediction crop"
                )
                async with api() as db:
                    assert await prediction_inputs.check_metadata(db, inputs) == (
                        inputs.record
                    )
                    async with owner() as observer:
                        saved = await observer.get(PredictionInputLease, identifier)
                        receipt = next(iter(saved.inventory["objects"].values()))
                        assert (
                            saved.state == "active" and receipt["state"] == "verified"
                        )
                        assert receipt["version_id"] != "null"
                        assert receipt["version_id"] in signed
                        assert len(storage.objects) == 1
                        with pytest.raises(DBAPIError) as busy:
                            await observer.execute(
                                select(PredictionInputLease.id)
                                .where(PredictionInputLease.id == identifier)
                                .with_for_update(nowait=True)
                            )
                        assert busy.value.orig.sqlstate == "55P03"
            async with owner.begin() as db:
                saved = await db.get(PredictionInputLease, identifier)
                assert saved.state == "deleted"
                assert next(iter(saved.inventory["objects"].values())) == receipt
                assert not storage.objects
                await db.delete(saved)
                await db.execute(
                    delete(StorageUsageMutation).where(
                        StorageUsageMutation.owner_id == identifier
                    )
                )
    finally:
        await engine.dispose()
