"""Real Registry dependencies must not be disrupted by fixture retirement."""

from __future__ import annotations

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
from tests.seed_owned_scenarios import A, _build_owned, _cleanup_owned
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
