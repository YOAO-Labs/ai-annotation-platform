"""Training deletion commits a durable cleanup intent before destructive IO."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.training_datasets import delete_version
from app.api.v1.training_runs import delete_run
from app.config import settings
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.db.models.user import User
from app.deps import get_db
from app.services import training_storage_cleanup as cleanup, training_run_storage
from app.services.storage import storage_service
from app.workers import sample_evaluations as worker
from tests.factory import create_user
from tests.test_training_runs import ready_dataset


async def terminal_record(db, owner, kind):
    project, dataset = await ready_dataset(db, owner)
    if kind == "datasets":
        return project, dataset
    run = TrainingRun(
        project_id=project.id,
        creator_id=owner.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="f" * 64,
        name="Ended experiment",
        status="failed",
        config={},
        config_digest="a" * 64,
    )
    db.add(run)
    await db.flush()
    await training_run_storage.allocate(db, run)
    return project, run


@pytest.fixture(params=["datasets", "runs"])
async def committed_record(request, test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    kind = request.param
    async with factory() as db:
        owner = await create_user(
            db, "super_admin", f"delete-{uuid.uuid4()}@test.local", "Deletion"
        )
        project, record = await terminal_record(db, owner, kind)
        await db.commit()
    world = SimpleNamespace(
        factory=factory,
        kind=kind,
        owner=owner,
        project=project,
        record=record,
        prefix=f"projects/{project.id}/{kind}/{record.id}/",
        endpoint=delete_version if kind == "datasets" else delete_run,
    )
    try:
        yield world
    finally:
        async with factory() as db:
            for model, condition in (
                (
                    TrainingStorageDeletion,
                    TrainingStorageDeletion.prefix == world.prefix,
                ),
                (TrainingRun, TrainingRun.project_id == project.id),
                (
                    TrainingDatasetVersion,
                    TrainingDatasetVersion.project_id == project.id,
                ),
                (Project, Project.id == project.id),
                (User, User.id == owner.id),
            ):
                await db.execute(delete(model).where(condition))
            await db.commit()


async def test_commit_failure_never_purges_training_storage(
    committed_record, monkeypatch
):
    world = committed_record
    purge = Mock()
    monkeypatch.setattr(storage_service, "delete_prefix", purge)
    async with world.factory() as db:
        monkeypatch.setattr(
            db, "commit", AsyncMock(side_effect=RuntimeError("commit unavailable"))
        )
        with pytest.raises(RuntimeError, match="commit unavailable"):
            await world.endpoint(
                world.record.id, project=world.project, user=world.owner, db=db
            )
    purge.assert_not_called()
    async with world.factory() as db:
        record = await db.get(type(world.record), world.record.id)
        assert record is not None and record.status == world.record.status
        assert not await db.scalar(select(TrainingStorageDeletion.id))


async def test_storage_failure_keeps_intent_for_existing_maintenance_owner(
    committed_record, test_engine, app_module, monkeypatch
):
    world = committed_record
    original_bucket = world.record.storage_ref["bucket"]
    purge = Mock(side_effect=RuntimeError("storage unavailable"))
    warning = Mock()
    monkeypatch.setattr(storage_service, "delete_prefix", purge)
    monkeypatch.setattr(cleanup.log, "warning", warning)

    async def request_session():
        async with world.factory() as db:
            yield db

    monkeypatch.setitem(app_module.dependency_overrides, get_db, request_session)
    token = create_access_token(subject=str(world.owner.id), role="super_admin")
    transport = httpx.ASGITransport(app=app_module)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/projects/{world.project.id}/training-{world.kind}/{world.record.id}",
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 204, response.text
    async with world.factory() as db:
        assert await db.get(type(world.record), world.record.id) is None
        intent = await db.scalar(select(TrainingStorageDeletion))
        assert (intent.prefix, intent.bucket) == (world.prefix, original_bucket)
        assert intent.storage_ref == world.record.storage_ref
        assert intent.last_attempted_at is not None
        intent_id = intent.id
    warning.assert_called_once_with(
        "Training storage deletion deferred intent=%s", intent_id
    )
    # A further outage must retain the same committed intent. A changed runtime
    # bucket must never redirect old cleanup to another bucket.
    monkeypatch.setattr(storage_service, "training_bucket", "different-training")
    await worker.maintain(world.factory)
    async with world.factory() as db:
        assert await db.get(TrainingStorageDeletion, intent_id) is not None
    purge.side_effect = None
    # Exercise the registered task entry point, which creates its own engine.
    # Refuse to invoke it if runtime settings target another database.
    task_url = make_url(settings.database_url)
    assert (task_url.host, task_url.port, task_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    await asyncio.to_thread(worker.maintain_evaluations.run)
    assert purge.call_count == 3
    for call in purge.call_args_list:
        assert call.args == (world.prefix,) and call.kwargs == {
            "bucket": original_bucket
        }
    async with world.factory() as db:
        assert await db.get(TrainingStorageDeletion, intent_id) is None
        assert not await cleanup.purge_requested(db, intent_id)
    await worker.maintain(world.factory)
    assert purge.call_count == 3


@pytest.mark.parametrize("failed_commit", [2, 3])
async def test_cleanup_commit_failure_keeps_retryable_intent(
    committed_record, monkeypatch, failed_commit
):
    world = committed_record
    purge = Mock()
    monkeypatch.setattr(storage_service, "delete_prefix", purge)
    async with world.factory() as db:
        commit = db.commit
        commits = 0

        async def fail_acknowledgement():
            nonlocal commits
            commits += 1
            if commits == failed_commit:
                raise RuntimeError("cleanup commit unavailable")
            await commit()

        monkeypatch.setattr(db, "commit", fail_acknowledgement)
        assert (
            await world.endpoint(
                world.record.id, project=world.project, user=world.owner, db=db
            )
            is None
        )
    async with world.factory() as db:
        assert await db.get(type(world.record), world.record.id) is None
        assert await db.scalar(select(TrainingStorageDeletion.id)) is not None
    await worker.maintain(world.factory)
    assert purge.call_count == (1 if failed_commit == 2 else 2)
    async with world.factory() as db:
        assert not await db.scalar(select(TrainingStorageDeletion.id))


@pytest.mark.parametrize(
    "unresolved", [False, True], ids=["endpoint-changed", "legacy"]
)
@pytest.mark.parametrize("kind", ["datasets", "runs"])
async def test_unknown_training_owner_retains_intent_without_physical_delete(
    db_session, super_admin, monkeypatch, unresolved, kind
):
    from copy import deepcopy
    from app.services.training_dataset_bridge import TrainingDatasetError

    owner, _ = super_admin
    _, version = await terminal_record(db_session, owner, kind)
    ref = deepcopy(version.storage_ref)
    if not unresolved:
        ref["endpoint_url"] = "http://unverified.invalid:9000"
    intent = TrainingStorageDeletion(
        bucket=ref["bucket"],
        prefix=ref["prefix"],
        storage_ref=None if unresolved else ref,
    )
    db_session.add(intent)
    await db_session.flush()
    purge = Mock()
    monkeypatch.setattr(storage_service, "delete_prefix", purge)
    with pytest.raises(TrainingDatasetError):
        await cleanup.purge_requested(db_session, intent.id)
    purge.assert_not_called()
    assert await db_session.get(TrainingStorageDeletion, intent.id) is intent
    assert intent.last_attempted_at is not None
