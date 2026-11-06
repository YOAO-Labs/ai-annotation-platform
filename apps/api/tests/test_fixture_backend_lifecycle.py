"""Real Registry dependencies must not be disrupted by fixture retirement."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import uuid

import pytest
from sqlalchemy import delete, select

from app.api.v1 import _test_seed
from app.db.models.fixture_run import FixtureRun
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.project import Project
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.user import User
from tests.factory import create_user
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    owner_fault_http as owner_fault_http,
)
from tests.seed_owned_scenarios import A, B, _build_owned, _cleanup_owned
from tests.test_trained_models import seed_source

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("arrival", ["existing", "during_cleanup"])
async def test_foreign_learning_deployment_is_preserved_before_fixture_drain(
    fixture_and_fenced_http, owner_fault_http, committed_owner, monkeypatch, arrival
):
    client, db = fixture_and_fenced_http, committed_owner
    run = client.owner_suite.new_run(A)
    fixture = await _build_owned(client, run)
    registry_id = uuid.UUID(fixture["ml_backend_id"])
    actor = await create_user(
        db,
        "super_admin",
        f"fixture-learning-{uuid.uuid4().hex}@example.test",
        "Independent Learning owner",
    )
    project, training, attempt = await seed_source(db, actor)
    model = TrainedModelVersion(
        project_id=project.id,
        creator_id=actor.id,
        run_id=training.id,
        attempt_id=attempt.id,
        request_id=uuid.uuid4(),
        request_digest="1" * 64,
        name="Independent model",
        framework="ultralytics",
        framework_version="8.4.177",
        artifact={"sha256": "2" * 64, "size_bytes": 100, "key": "owned/best.pt"},
        classes=["car"],
        bindings=["car"],
        binding_digest="3" * 64,
        provenance={},
    )
    db.add(model)
    await db.flush()
    deployment = ModelDeployment(
        id=uuid.uuid4(),
        model_version_id=model.id,
        registry_id=registry_id,
        status="ready",
    )
    if arrival == "existing":
        db.add(deployment)
    await db.commit()
    project_id, actor_id, deployment_id = project.id, actor.id, deployment.id
    model_id, training_id, dataset_id = model.id, training.id, training.dataset_id
    if arrival == "during_cleanup":
        prepare = _test_seed._prepare_fixture_video_cleanup

        async def prepare_then_reference(*args, **kwargs):
            result = await prepare(*args, **kwargs)
            db.add(deployment)
            await db.commit()
            return result

        monkeypatch.setattr(
            _test_seed, "_prepare_fixture_video_cleanup", prepare_then_reference
        )
    query = (
        select(
            MLBackendPoolMember.traffic_state,
            MLBackendServicePool.routing_generation,
        )
        .select_from(MLBackendPoolMember)
        .join(
            MLBackendServicePool, MLBackendServicePool.id == MLBackendPoolMember.pool_id
        )
        .where(MLBackendPoolMember.registry_id == registry_id)
    )
    try:
        before = (await db.execute(query)).one()
        assert before.traffic_state == "active"
        await db.rollback()
        response = None
        failure = None
        try:
            response = await owner_fault_http.post(
                "/api/v1/__test/seed/owned-cleanup", json=run.payload()
            )
        except Exception as error:
            failure = error
        after = (await db.execute(query)).one()
        assert after == before, (
            "Foreign Learning dependency changed routing before rejection"
        )
        assert failure is None, repr(failure)
        assert response.status_code == 409, response.text
        assert (await db.get(ModelDeployment, deployment_id)).status == "ready"
        assert await db.get(Project, uuid.UUID(fixture["project_id"])) is not None
    finally:
        await db.rollback()
        await db.execute(
            delete(ModelDeployment).where(ModelDeployment.id == deployment_id)
        )
        await db.execute(
            delete(TrainedModelVersion).where(TrainedModelVersion.id == model_id)
        )
        await db.execute(delete(TrainingRun).where(TrainingRun.id == training_id))
        await db.execute(
            delete(TrainingDatasetVersion).where(
                TrainingDatasetVersion.id == dataset_id
            )
        )
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(User).where(User.id == actor_id))
        await db.commit()
        response = await _cleanup_owned(client, run)
        assert response.status_code == 200, response.text


async def test_suite_cleanup_retries_provider_close_after_durable_retirement(
    fixture_and_fenced_http, owner_fault_http, committed_owner, monkeypatch
):
    suite = fixture_and_fenced_http.owner_suite
    run = suite.new_run(A)
    await _build_owned(owner_fault_http, run)
    providers = owner_fault_http.fixture_app.state.fixture_backends
    provider = providers.runs[run.run_id]
    close = provider.close

    class CloseInterrupted(Exception):
        pass

    async def interrupt():
        raise CloseInterrupted()

    monkeypatch.setattr(provider, "close", interrupt)
    try:
        with pytest.raises(CloseInterrupted):
            await _cleanup_owned(owner_fault_http, run)
        assert (await committed_owner.get(FixtureRun, run.run_id)).state == "retired"
        assert run.run_id in providers.runs
        assert provider.process.returncode is None
        await committed_owner.rollback()
    finally:
        monkeypatch.setattr(provider, "close", close)

    response = await owner_fault_http.post(
        "/api/v1/__test/seed/cleanup", json={"suite_id": str(run.suite_id)}
    )
    assert response.status_code == 200, response.text
    assert provider.process.returncode == 0
    assert run.run_id not in providers.runs
    assert (await committed_owner.get(FixtureRun, run.suite_id)).state == "retired"


@pytest.mark.parametrize("reader", ["none", "released", "held"])
async def test_suite_reader_budget_does_not_expire_between_children(
    fixture_and_fenced_http, owner_fault_http, test_engine, monkeypatch, reader
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application import fixture_prediction_retirement as retirement
    from app.services import fixture_original_read, original_sources

    suite = fixture_and_fenced_http.owner_suite
    runs = [suite.new_run(A), suite.new_run(B)]
    worlds = {run.run_id: await _build_owned(owner_fault_http, run) for run in runs}
    first, second = sorted(runs, key=lambda run: run.run_id)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    preflight_blocked = asyncio.Event()
    release_preflight = asyncio.Event()
    second_waiting = asyncio.Event()
    real_retirement = retirement.retire_fixture_prediction_work
    real_wait = retirement.wait_for_fixture_original_readers
    delayed = False

    async def delay_real_routing(**kwargs):
        nonlocal delayed
        result = await real_retirement(**kwargs)
        if kwargs["run_id"] == first.run_id and not delayed:
            delayed = True
            preflight_blocked.set()
            await release_preflight.wait()
        return result

    async def observe_real_reader(factory, run_id, **kwargs):
        if run_id == second.run_id:
            second_waiting.set()
        return await real_wait(factory, run_id, **kwargs)

    monkeypatch.setattr(
        retirement, "retire_fixture_prediction_work", delay_real_routing
    )
    monkeypatch.setattr(
        retirement, "wait_for_fixture_original_readers", observe_real_reader
    )
    async with AsyncExitStack() as stack:
        if reader != "none":
            db = await stack.enter_async_context(factory())
            world = worlds[second.run_id]
            actor = await db.scalar(
                select(User.id).where(User.email == world["admin_email"])
            )
            assert actor is not None
            source = await original_sources.resolve_task(
                db, uuid.UUID(world["task_ids"][0])
            )
            await db.rollback()
            await stack.enter_async_context(
                fixture_original_read.admitted(
                    db, source, actor, original_sources.read_plan(source).spec
                )
            )
        cleanup = asyncio.create_task(
            owner_fault_http.post(
                "/api/v1/__test/seed/cleanup", json={"suite_id": str(suite.suite_id)}
            )
        )
        try:
            await asyncio.wait_for(preflight_blocked.wait(), 20)
            late = await owner_fault_http.post(
                "/api/v1/__test/seed/owned", json=first.payload()
            )
            assert late.status_code == 409, late.text
            # Real Routing preflight outlasts both the reader wait and the former
            # whole-suite transport limit; later readers still get their own wait.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), 5.1)
            release_preflight.set()
            await asyncio.wait_for(second_waiting.wait(), 10)
            if reader != "none":
                async with test_engine.connect() as observer:
                    empty = await observer.scalar(
                        text(
                            "SELECT aap_assets_v1.fixture_original_readers_empty(:run)"
                        ),
                        {"run": second.run_id},
                    )
                assert empty is False and not cleanup.done()
                if reader == "released":
                    await stack.aclose()
            response = await asyncio.wait_for(cleanup, 30)
            if reader == "held":
                assert response.status_code == 409, response.text
                assert response.json()["detail"] == {
                    "code": "fixture_prediction_retirement_blocked",
                    "reason": "original_reader_pending",
                }
                async with factory() as db:
                    for run in runs:
                        assert (await db.get(FixtureRun, run.run_id)).state == "sealing"
                        assert (
                            await db.get(
                                Project, uuid.UUID(worlds[run.run_id]["project_id"])
                            )
                            is not None
                        )
                await stack.aclose()
                response = await owner_fault_http.post(
                    "/api/v1/__test/seed/cleanup",
                    json={"suite_id": str(suite.suite_id)},
                )
            assert response.status_code == 200, response.text
        finally:
            release_preflight.set()
            if not cleanup.done():
                cleanup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cleanup
    async with factory() as db:
        for run in runs:
            assert (await db.get(FixtureRun, run.run_id)).state == "retired"
