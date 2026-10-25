"""Real-transaction execution fencing, recovery and uncertain publication windows."""

import asyncio
import copy
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.sample_evaluation import (
    SampleEvaluationRun as Run,
    SampleEvaluationAttempt as Attempt,
    SampleEvaluationSample as Sample,
)
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.db.models.user import User
from app.services import sample_evaluations as service
from app.services.training_dataset_bridge import content_digest
from app.workers import sample_evaluations as worker
from tests.factory import create_user
from tests.sample_evaluation_fixtures import (
    Objects,
    complete_rows,
    evaluation,
    prediction,
    seed,
)


@pytest.fixture
async def world(test_engine, monkeypatch):
    from tests.training_consumer_fixtures import mock_legacy_transport

    mock_legacy_transport(monkeypatch)
    objects = Objects()
    monkeypatch.setattr(service.storage, "client", objects)
    monkeypatch.setattr(service.storage, "delete_prefix", objects.delete_prefix)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        owner = await create_user(
            db, "super_admin", f"eval-{uuid.uuid4()}@test.local", "Evaluation"
        )
        fixture = await seed(db, owner, objects, count=3)
        run, attempt = await evaluation(db, owner, fixture)
        pool_id = await db.scalar(
            select(ProjectMLBackendPool.pool_id).where(
                ProjectMLBackendPool.project_id == fixture.project.id
            )
        )
        await db.commit()
    result = SimpleNamespace(
        factory=maker,
        owner=owner,
        fixture=fixture,
        run=run,
        attempt=attempt,
        objects=objects,
    )
    try:
        yield result
    finally:
        # This suite intentionally commits rows; remove every owned dependency.
        async with maker() as db:
            pid, uid, registry = fixture.project.id, owner.id, fixture.backend.id
            for model, condition in (
                (Notification, Notification.user_id == uid),
                (Run, Run.project_id == pid),
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
        objects.objects.clear()


async def test_duplicate_delivery_has_one_claim_and_stale_writer_cannot_checkpoint(
    world,
):
    factory, run, attempt = world.factory, world.run, world.attempt
    claims = await asyncio.gather(
        worker.claim(factory, attempt.id, "delivery-a"),
        worker.claim(factory, attempt.id, "delivery-b"),
    )
    assert sum(claim is not None for claim in claims) == 1
    claim_id = next(claim for claim in claims if claim)
    async with factory() as db:
        current, old = await worker.fenced(db, attempt.id, claim_id)
        row = await db.get(Sample, (run.id, 0))
        _png, preprocessing = service.prepare_input(
            current, world.fixture.dataset, current.spec["samples"][0]
        )
        row.request = service.request_identity(current.spec, row, preprocessing)
        result = prediction(current, row)
        current.status = old.status = "lost"
        old.claim_id = None
        new = await service.new_attempt(db, current)
        await db.commit()
    with pytest.raises(worker.FencedOut):
        await worker.checkpoint(factory, attempt.id, claim_id, 0, result)
    async with factory() as db:
        assert (await db.get(Run, run.id)).current_attempt_id == new.id
        assert (await db.get(Sample, (run.id, 0))).evidence is None


async def test_execution_reuses_checkpoint_and_lost_remote_receipt_on_new_attempt(
    world, monkeypatch
):
    native, cache = [], {}
    lost_once = False

    async def infer(factory, run, row, _png, _attempt_id, _claim_id):
        nonlocal lost_once
        # A second connection can acquire the domain row during native IO.
        async with factory() as db:
            assert (
                await db.scalar(
                    select(Run.id).where(Run.id == run.id).with_for_update(nowait=True)
                )
                == run.id
            )
        if row.request_id not in cache:
            native.append(row.ordinal)
            cache[row.request_id] = prediction(run, row)
        if row.ordinal == 1 and not lost_once:
            lost_once = True
            raise worker.RemoteUncertain
        return cache[row.request_id]

    monkeypatch.setattr(worker, "obtain_result", infer)
    await worker.execute(world.factory, world.attempt.id, "first")
    async with world.factory() as db:
        run = await db.get(Run, world.run.id)
        assert run.status == "lost"
        rows = await service.sample_rows(db, run)
        original = copy.deepcopy(rows[0].evidence)
        request_ids = [row.request_id for row in rows]
        parent = run.current_attempt_id
        new = await service.new_attempt(db, run)
        await db.commit()
    await worker.execute(world.factory, new.id, "recovery")
    async with world.factory() as db:
        run = await db.get(Run, world.run.id)
        assert run.status == "succeeded", run.error_message
        rows = await service.sample_rows(db, run)
        assert native == [0, 1, 2]
        assert rows[0].evidence == original and rows[0].produced_attempt_id == parent
        assert [row.request_id for row in rows] == request_ids
        report = await service.read_report(db, run, await db.get(Attempt, new.id))
        assert report["complete"] and report["aggregate"]["fn"] == 2
        assert (await db.get(Attempt, parent)).status == "lost"


async def test_completed_receipt_is_recovered_without_routed_inference_or_new_grant(
    world, monkeypatch
):
    async with world.factory() as db:
        run = await db.get(Run, world.run.id)
        row = await db.get(Sample, (run.id, 0))
        png, preprocessing = service.prepare_input(
            run, world.fixture.dataset, run.spec["samples"][0]
        )
        row.request = service.request_identity(run.spec, row, preprocessing)
        result = prediction(run, row)
        response = {
            "results": [
                {"task": result.task_id, "result": result.result, "meta": result.meta}
            ]
        }
        await db.commit()
    private = SimpleNamespace(
        managed_evaluation_request=AsyncMock(
            return_value={
                "state": "completed",
                "response": response,
                "response_digest": content_digest(response),
            }
        )
    )
    monkeypatch.setattr(worker, "private_client", AsyncMock(return_value=private))
    new_grant = Mock(side_effect=AssertionError("Receipt must not ask for a new grant"))
    monkeypatch.setattr(worker.managed, "build_gpu_dispatch_context_factory", new_grant)
    recovered = await worker.obtain_result(
        world.factory, run, row, png, world.attempt.id, uuid.uuid4()
    )
    assert recovered.meta == result.meta and not new_grant.called


async def test_cancel_stays_pending_while_native_busy_and_seals_before_late_post(
    world, monkeypatch
):
    claim_id = await worker.claim(world.factory, world.attempt.id, "cancel")
    async with world.factory() as db:
        run, _attempt = await worker.fenced(db, world.attempt.id, claim_id)
        row = await db.get(Sample, (run.id, 0))
        _png, preprocessing = service.prepare_input(
            run, world.fixture.dataset, run.spec["samples"][0]
        )
        row.request, row.status = (
            service.request_identity(run.spec, row, preprocessing),
            "inflight",
        )
        await service.request_cancel(db, run)
        await db.commit()
    busy = True
    sealed = []

    async def receipt(_client, row, *, seal):
        assert seal
        if busy:
            raise HTTPException(409, detail={"reason": "evaluation_request_busy"})
        sealed.append(row.request_id)
        return {"state": "sealed"}

    monkeypatch.setattr(worker, "remote_receipt", receipt)
    assert not await worker.settle_cancel(world.factory, run.id)
    async with world.factory() as db:
        assert (await db.get(Run, run.id)).status == "cancelling"
        assert (await db.get(AsyncJob, world.attempt.async_job_id)).status == "running"
    busy = False
    assert await worker.settle_cancel(world.factory, run.id)
    async with world.factory() as db:
        current = await db.get(Run, run.id)
        assert current.status == "cancelled" and sealed == [row.request_id]
        report = await service.read_report(
            db, current, await db.get(Attempt, world.attempt.id)
        )
        assert not report["complete"] and report["aggregate"] is None
        assert report["pending_samples"] == 3
    with pytest.raises(worker.FencedOut):
        async with world.factory() as db:
            await worker.fenced(db, world.attempt.id, claim_id)


async def test_lost_commit_acknowledgement_keeps_the_published_report(world):
    claim_id = await worker.claim(world.factory, world.attempt.id, "commit-ack")
    async with world.factory() as db:
        run, attempt = await worker.fenced(db, world.attempt.id, claim_id)
        await complete_rows(db, run, attempt, world.fixture)
        await db.commit()

    @asynccontextmanager
    async def uncertain_factory():
        async with world.factory() as db:
            commit = db.commit

            async def commit_then_lose_ack():
                publishing = any(
                    isinstance(row, Attempt) and row.report
                    for row in db.identity_map.values()
                )
                await commit()
                if publishing:
                    raise ConnectionError("Commit ACK lost after success")

            db.commit = commit_then_lose_ack
            yield db

    with pytest.raises(ConnectionError, match="ACK lost"):
        await worker.finish(uncertain_factory, world.attempt.id, claim_id)
    async with world.factory() as db:
        run, attempt = (
            await db.get(Run, world.run.id),
            await db.get(Attempt, world.attempt.id),
        )
        assert (
            run.status == "succeeded" and attempt.report["key"] in world.objects.objects
        )
        assert (await service.read_report(db, run, attempt))["complete"]
        assert not world.objects.deleted


async def test_competing_cancel_publish_and_revocation_cannot_delete_a_committed_report(
    world, monkeypatch
):
    async with world.factory() as db:
        await service.request_cancel(db, await db.get(Run, world.run.id))
        await db.commit()
    upload = service.upload_report
    both_started, release_second = threading.Event(), threading.Event()
    counter, mutex = [], threading.Lock()

    def synchronized_upload(*args, **kwargs):
        artifact = upload(*args, **kwargs)
        with mutex:
            counter.append(artifact)
            index = len(counter)
            if index == 2:
                both_started.set()
        assert both_started.wait(timeout=5)
        if index == 2:
            assert release_second.wait(timeout=5)
        return artifact

    monkeypatch.setattr(service, "upload_report", synchronized_upload)
    tasks = [
        asyncio.create_task(worker.settle_cancel(world.factory, world.run.id))
        for _ in range(2)
    ]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(both_started.wait), timeout=5)
        done, _pending = await asyncio.wait(
            tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(done) == 1 and next(iter(done)).result()
        async with world.factory() as db:
            owner = await db.get(User, world.owner.id)
            owner.is_active = False
            await db.commit()
    finally:
        release_second.set()
    results = await asyncio.gather(*tasks)
    assert sorted(results) == [False, True]
    async with world.factory() as db:
        attempt = await db.get(Attempt, world.attempt.id)
        assert (
            attempt.report["key"] in world.objects.objects and not world.objects.deleted
        )


async def test_crash_signal_preserves_cancel_intent_and_revocation_reason(world):
    async with world.factory() as db:
        run, attempt = (
            await db.get(Run, world.run.id),
            await db.get(Attempt, world.attempt.id),
        )
        run.status = attempt.status = "cancelling"
        run.error_code = attempt.error_code = "authority_revoked"
        run.error_message = attempt.error_message = "Current authority was revoked"
        attempt.claim_id = uuid.uuid4()
        job = await db.get(AsyncJob, attempt.async_job_id)
        await service.signal_interrupted(db, job, cancelled=False)
        assert run.status == "cancelling" and run.error_code == "authority_revoked"
        assert attempt.claim_id is None and job.status == "pending"


async def test_deletion_intent_blocks_resurrection_and_purge_io_holds_no_domain_lock(
    world, monkeypatch
):
    async with world.factory() as db:
        run, attempt = (
            await db.get(Run, world.run.id),
            await db.get(Attempt, world.attempt.id),
        )
        run.status = attempt.status = "failed"
        run.deletion_requested_at = service.now()
        await db.commit()
    original = service.storage.delete_prefix
    monkeypatch.setattr(
        service.storage,
        "delete_prefix",
        Mock(side_effect=RuntimeError("S3 temporarily unavailable")),
    )
    async with world.factory() as db:
        with pytest.raises(RuntimeError):
            await service.purge_requested(db, world.run.id)
    async with world.factory() as db:
        assert (await db.get(Run, world.run.id)).deletion_requested_at
    monkeypatch.setattr(service.storage, "delete_prefix", original)
    active = None

    def assert_no_transaction(prefix, **kwargs):
        assert not active.in_transaction()
        original(prefix, **kwargs)

    monkeypatch.setattr(service.storage, "delete_prefix", assert_no_transaction)
    async with world.factory() as db:
        active = db
        assert await service.purge_requested(db, world.run.id)
        assert await db.get(Run, world.run.id) is None
    import hashlib

    sha = hashlib.sha256(b"late").hexdigest()
    late = {
        "key": service.report_prefix(world.run) + f"{world.attempt.id}/{sha}.json",
        "sha256": sha,
    }
    world.objects.put_object(Bucket="test", Key=late["key"], Body=b"late")
    await worker.cleanup_candidate(
        world.factory,
        world.run.id,
        world.attempt.id,
        late,
        storage_ref=world.run.storage_ref,
    )
    assert late["key"] not in world.objects.objects


@pytest.mark.parametrize("blocking_state", ["lost", "cancelling"])
async def test_maintenance_rotates_past_more_than_one_hundred_blocked_runs(
    world, monkeypatch, blocking_state
):
    async with world.factory() as db:
        current = await db.get(Run, world.run.id)
        current.created_at = service.now() + timedelta(hours=1)
        owner = await db.get(User, world.owner.id)
        for _ in range(101):
            run, attempt = await evaluation(db, owner, world.fixture)
            run.status = attempt.status = blocking_state
        await db.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr(worker, "dispatch", dispatch)
    monkeypatch.setattr(worker, "settle_cancel", AsyncMock(return_value=False))
    await worker.maintain(world.factory)
    await worker.maintain(world.factory)
    assert world.run.id in {call.args[1].id for call in dispatch.await_args_list}
