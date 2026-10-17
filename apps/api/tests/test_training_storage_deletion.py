"""Training deletion commits a durable cleanup intent before destructive IO."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.training_datasets import delete_version
from app.api.v1.training_runs import delete_run
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.db.models.user import User
from app.deps import get_db
from app.services import training_storage_cleanup as cleanup
from app.services.storage import storage_service
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
        actor_argument={"user": owner}
        if kind == "datasets"
        else {"actor_id": owner.id},
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
    objects = {"artifact": b"original"}

    def purge(*_args, **_kwargs):
        objects.clear()

    monkeypatch.setattr(storage_service, "delete_prefix", purge)
    async with world.factory() as db:
        monkeypatch.setattr(
            db, "commit", AsyncMock(side_effect=RuntimeError("commit unavailable"))
        )
        with pytest.raises(RuntimeError):
            await world.endpoint(
                world.record.id, project=world.project, db=db, **world.actor_argument
            )
    assert objects == {"artifact": b"original"}
    async with world.factory() as db:
        record = await db.get(type(world.record), world.record.id)
        assert record is not None and record.status == world.record.status
        assert not await db.scalar(
            select(TrainingStorageDeletion.id).where(
                TrainingStorageDeletion.prefix == world.prefix
            )
        )


async def test_storage_failure_keeps_intent_for_existing_maintenance_owner(
    committed_record, app_module, monkeypatch
):
    world = committed_record
    original_bucket = storage_service.training_bucket
    other_bucket = "different-training"
    key = world.prefix + "artifact"
    objects = {
        original_bucket: {key: b"original"},
        other_bucket: {key: b"unrelated"},
    }
    unavailable = True

    def purge(prefix, *, bucket):
        if unavailable:
            raise RuntimeError("storage unavailable")
        for object_key in list(objects[bucket]):
            if object_key.startswith(prefix):
                del objects[bucket][object_key]

    monkeypatch.setattr(storage_service, "delete_prefix", purge)

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
        intent = await db.scalar(
            select(TrainingStorageDeletion).where(
                TrainingStorageDeletion.prefix == world.prefix
            )
        )
        assert (intent.prefix, intent.bucket) == (world.prefix, original_bucket)
        assert intent.last_attempted_at is not None
        intent_id = intent.id
    assert objects[original_bucket] == {key: b"original"}
    # A changed runtime bucket must not redirect the committed cleanup intent.
    monkeypatch.setattr(storage_service, "training_bucket", other_bucket)
    await cleanup.maintain(world.factory)
    async with world.factory() as db:
        assert await db.get(TrainingStorageDeletion, intent_id) is not None
    unavailable = False
    await cleanup.maintain(world.factory)
    assert objects[original_bucket] == {}
    assert objects[other_bucket] == {key: b"unrelated"}
    async with world.factory() as db:
        assert await db.get(TrainingStorageDeletion, intent_id) is None
        assert not await cleanup.purge_requested(db, intent_id)
    await cleanup.maintain(world.factory)
    assert objects[other_bucket] == {key: b"unrelated"}


@pytest.mark.parametrize("failed_commit", [2, 3])
async def test_cleanup_commit_failure_keeps_retryable_intent(
    committed_record, monkeypatch, failed_commit
):
    world = committed_record
    objects = {"artifact": b"original"}

    def purge(*_args, **_kwargs):
        objects.clear()

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
        await world.endpoint(
            world.record.id, project=world.project, db=db, **world.actor_argument
        )
    # The first cleanup commit precedes IO; the second acknowledges its result.
    assert objects == ({"artifact": b"original"} if failed_commit == 2 else {})
    async with world.factory() as db:
        assert await db.get(type(world.record), world.record.id) is None
        assert await db.scalar(
            select(TrainingStorageDeletion.id).where(
                TrainingStorageDeletion.prefix == world.prefix
            )
        )
    await cleanup.maintain(world.factory)
    assert objects == {}
    async with world.factory() as db:
        assert not await db.scalar(
            select(TrainingStorageDeletion.id).where(
                TrainingStorageDeletion.prefix == world.prefix
            )
        )
