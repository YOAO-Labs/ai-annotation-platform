"""Real transaction fences, durable debits and remote stop proof."""

import asyncio
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.active_learning import (
    ActiveLearningAttempt as Attempt,
    ActiveLearningCandidate as Candidate,
    ActiveLearningRound as Round,
)
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.db.models.user import User
from app.services import active_learning as service, managed_inference as managed
from app.services.training_dataset_bridge import content_digest
from app.workers import active_learning as worker
from tests.active_learning_fixtures import create, seed
from tests.factory import create_user
from tests.sample_evaluation_fixtures import prediction


@pytest.fixture
async def world(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        owner = await create_user(
            db, "super_admin", f"active-{uuid.uuid4()}@test.local", "Selection"
        )
        fixture = await seed(db, owner, monkeypatch)
        round, attempt, _ = await create(db, owner, fixture)
        pool_id = await db.scalar(
            select(ProjectMLBackendPool.pool_id).where(
                ProjectMLBackendPool.project_id == fixture.project.id
            )
        )
        await db.commit()
    value = SimpleNamespace(
        factory=maker, owner=owner, fixture=fixture, round=round, attempt=attempt
    )
    try:
        yield value
    finally:
        async with maker() as db:
            pid, uid, registry = fixture.project.id, owner.id, fixture.backend.id
            for model, condition in (
                (Notification, Notification.user_id == uid),
                (Round, Round.project_id == pid),
                (Task, Task.project_id == pid),
                (Dataset, Dataset.id.in_(fixture.dataset_ids)),
                (TaskBatch, TaskBatch.project_id == pid),
                (AsyncJob, AsyncJob.project_id == pid),
                (ModelDeployment, ModelDeployment.registry_id == registry),
                (TrainedModelVersion, TrainedModelVersion.project_id == pid),
                (
                    TrainingAttempt,
                    TrainingAttempt.run_id.in_(
                        select(TrainingRun.id).where(TrainingRun.project_id == pid)
                    ),
                ),
                (TrainingRun, TrainingRun.project_id == pid),
                (TrainingDatasetVersion, TrainingDatasetVersion.project_id == pid),
                (ProjectMLBackendPool, ProjectMLBackendPool.project_id == pid),
                (MLBackendPoolMember, MLBackendPoolMember.registry_id == registry),
                (MLBackendServicePool, MLBackendServicePool.id == pool_id),
                (MLBackendRegistry, MLBackendRegistry.id == registry),
                (Project, Project.id == pid),
                (ApiKey, ApiKey.user_id == uid),
                (User, User.id == uid),
            ):
                await db.execute(delete(model).where(condition))
            await db.commit()
        fixture.objects.objects.clear()
        assert all(body.closed for body in fixture.originals.bodies)
        fixture.originals.objects.clear()


def receipt(row, state, result=None):
    value = {**row.request["evaluation"], "schema": 1, "state": state}
    if result is not None:
        response = {
            "results": [
                {"task": result.task_id, "result": result.result, "meta": result.meta}
            ]
        }
        value.update(response=response, response_digest=content_digest(response))
    return value


async def prepared(world, claim_id, ordinal=0):
    async with world.factory() as db:
        round, _ = await worker.fenced(db, world.attempt.id, claim_id)
        row = await db.get(Candidate, (round.id, ordinal))
        sample = round.spec["samples"][ordinal]
        png = await service.prepare_image(db, round, sample)
        row.request = managed.request_identity(round.spec, row, sample["preprocessing"])
        row.status, row.media = "inflight", service.media_descriptor(round, sample)
        await db.commit()
        return round, row, png


async def test_duplicate_claim_and_late_checkpoint_are_fenced(world):
    claims = await asyncio.gather(
        worker.claim(world.factory, world.attempt.id, "one"),
        worker.claim(world.factory, world.attempt.id, "two"),
    )
    assert sum(value is not None for value in claims) == 1
    claim_id = next(value for value in claims if value)
    round, row, _ = await prepared(world, claim_id)
    result = prediction(round, row)
    async with world.factory() as db:
        current = await db.get(Round, round.id)
        current.status = "lost"
        await service.new_attempt(db, current)
        await db.commit()
    with pytest.raises(worker.FencedOut):
        await worker.checkpoint(world.factory, world.attempt.id, claim_id, 0, result)
    async with world.factory() as db:
        assert (await db.get(Candidate, (round.id, 0))).evidence is None


async def test_timeout_debit_is_retained_on_retry_and_original_identity_never_refreshes(
    world, monkeypatch
):
    claim_id = await worker.claim(world.factory, world.attempt.id, "first")
    round, row, png = await prepared(world, claim_id)
    original = copy.deepcopy(row.request)
    monkeypatch.setattr(managed, "private_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        managed,
        "remote_receipt",
        AsyncMock(
            side_effect=lambda _client, candidate, **_kw: receipt(candidate, "missing")
        ),
    )
    monkeypatch.setattr(
        managed, "predict", AsyncMock(side_effect=TimeoutError("transport uncertain"))
    )
    with pytest.raises(worker.RemoteUncertain):
        await worker.obtain_result(
            world.factory, round, row, png, world.attempt.id, claim_id
        )
    await worker.mark_lost(world.factory, world.attempt.id, claim_id)
    async with world.factory() as db:
        current = await db.get(Round, round.id)
        assert current.inference_dispatches == 1
        newer = await service.new_attempt(db, current)
        await db.commit()
    new_claim = await worker.claim(world.factory, newer.id, "retry")
    async with world.factory() as db:
        current, _ = await worker.fenced(db, newer.id, new_claim)
        candidate = await db.get(Candidate, (round.id, 0))
        assert candidate.request == original
        assert current.spec["expires_at"] == round.spec["expires_at"]
    # A second dispatch is charged again, even for the same bound request.
    with pytest.raises(worker.RemoteUncertain):
        await worker.obtain_result(
            world.factory, current, candidate, png, newer.id, new_claim
        )
    async with world.factory() as db:
        assert (await db.get(Round, round.id)).inference_dispatches == 2


async def test_completed_receipt_recovers_without_dispatch_or_new_budget(
    world, monkeypatch
):
    claim_id = await worker.claim(world.factory, world.attempt.id, "completed")
    round, row, png = await prepared(world, claim_id)
    monkeypatch.setattr(managed, "private_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        managed,
        "remote_receipt",
        AsyncMock(return_value=receipt(row, "completed", prediction(round, row))),
    )
    predict = AsyncMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(managed, "predict", predict)
    result = await worker.obtain_result(
        world.factory, round, row, png, world.attempt.id, claim_id
    )
    assert (
        str(result.task_id) == round.spec["samples"][0]["task_id"]
        and not predict.called
    )
    async with world.factory() as db:
        assert (await db.get(Round, round.id)).inference_dispatches == 0


async def test_cancel_busy_receipt_stays_nonterminal_and_revocation_still_seals(
    world, monkeypatch
):
    claim_id = await worker.claim(world.factory, world.attempt.id, "cancel")
    round, row, _ = await prepared(world, claim_id)
    async with world.factory() as db:
        current = await db.get(Round, round.id)
        await service.request_cancel(
            db, current, code="authority_revoked", message="Revoked"
        )
        owner = await db.get(User, world.owner.id)
        owner.is_active = False
        await db.commit()
    monkeypatch.setattr(managed, "private_client", AsyncMock(return_value=object()))
    remote = AsyncMock(
        side_effect=HTTPException(409, detail={"reason": "evaluation_request_busy"})
    )
    monkeypatch.setattr(managed, "remote_receipt", remote)
    assert not await worker.settle_cancel(world.factory, round.id)
    async with world.factory() as db:
        current = await db.get(Round, round.id)
        job = await db.get(AsyncJob, current.async_job_id)
        assert current.status == "cancelling" and job.status != "cancelled"
    remote.side_effect = None
    remote.return_value = receipt(row, "sealed")
    assert await worker.settle_cancel(world.factory, round.id)
    assert remote.call_args.kwargs["seal"]
    async with world.factory() as db:
        current = await db.get(Round, round.id)
        assert current.status == "failed" and current.error_code == "authority_revoked"


async def test_prepost_budget_is_hard_cap_and_original_expiry_cannot_dispatch(
    world, monkeypatch
):
    claim_id = await worker.claim(world.factory, world.attempt.id, "cap")
    async with world.factory() as db:
        current = await db.get(Round, world.round.id)
        current.inference_dispatches = current.spec["max_inference_dispatches"] - 1
        await db.commit()
    await worker.debit(world.factory, world.attempt.id, claim_id)
    with pytest.raises(worker.RoundFailure, match="预算"):
        await worker.debit(world.factory, world.attempt.id, claim_id)
    monkeypatch.setattr(worker.time, "time", lambda: world.round.spec["expires_at"] + 1)
    with pytest.raises(worker.RoundFailure, match="窗口"):
        await worker.debit(world.factory, world.attempt.id, claim_id)


async def test_execute_publishes_only_verified_complete_evidence(world, monkeypatch):
    monkeypatch.setattr(managed, "private_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        managed,
        "remote_receipt",
        AsyncMock(side_effect=lambda _client, row, **_kw: receipt(row, "missing")),
    )
    observed = []

    async def native(_db, factory, **kwargs):
        async with factory() as check:
            current = await check.get(Round, world.round.id)
            observed.append(current.inference_dispatches)
        # The durable debit must precede every actual native dispatch.
        assert observed[-1] == len(observed)
        frozen = SimpleNamespace(spec=kwargs["spec"])
        return prediction(frozen, kwargs["row"])

    monkeypatch.setattr(managed, "predict", native)
    await worker.execute(world.factory, world.attempt.id, "success")
    async with world.factory() as db:
        current = await db.get(Round, world.round.id)
        attempt = await db.get(Attempt, world.attempt.id)
        assert current.status == "succeeded" and current.inference_dispatches == 6
        report = await service.read_report(db, current, attempt)
        assert report["missing_signal_count"] == 6
        assert sum(item["recommended"] for item in report["ranking"]) == 4
        assert (await db.get(AsyncJob, current.async_job_id)).status == "completed"
