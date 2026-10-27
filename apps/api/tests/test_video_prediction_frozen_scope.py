"""Exercise frozen selection through the actual Worker's SQL admission path.

Canonical video facts, original JWT authority and the execution snapshot are
real DB fixtures. No physical video qualification is claimed: stop at the
progress boundary after the Worker materializes its selected rows, before any
source owner or model transport can open. Mid-admission mutations use actual SQL
between the initial scope check and the final selection, rather than a copied
query/expression under test.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, update

from app.core.security import create_access_token
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.task import Task
from app.services import async_job as jobs
from app.services import video_prediction_references as references
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import register_item_asset
from app.services.prediction_execution import create_snapshot
from app.workers import tasks as worker
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_task

pytestmark = pytest.mark.asyncio


class _SelectionReached(BaseException):
    """Stop before source/model I/O without entering the ordinary failure path."""


@pytest.fixture
async def world(db_session, super_admin):
    actor, _token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    project.data_type, project.type_key = "video", "video-track"
    backend, pool = await create_registry_with_pool(
        db_session,
        name="frozen-video-scope",
        url="http://never-dispatch.invalid",
        state="connected",
        enabled_pool=True,
    )
    backend.health_meta = {
        "capabilities": {
            "task_contract_revision": 1,
            "models": [
                {
                    "id": "track",
                    "task": "tracker",
                    "supported_inputs": ["video"],
                    "supported_geometric_outputs": ["bbox"],
                    "resource_profile": {"device": "cpu", "batchable": True},
                }
            ],
        }
    }
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="scope-" + uuid.uuid4().hex[:12],
        name="Selection-only video facts",
        created_by=actor.id,
        data_type="video",
    )
    db_session.add(dataset)
    await db_session.flush()
    members = []
    for index in range(2):
        # Source facts are registered; no sentinel bytes are presented as a
        # decoded video or passed to a physical reader in this selection test.
        raw = f"selection metadata only {index}".encode()
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_path="frozen-selection/" + uuid.uuid4().hex + ".mp4",
            file_name=f"{index}.mp4",
            file_type="video",
            file_size=len(raw),
        )
        db_session.add(item)
        await db_session.flush()
        await register_item_asset(
            db_session,
            item,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )
        task = await create_task(db_session, project_id=project.id)
        task.file_type, task.file_name, task.file_path = (
            "video",
            item.file_name,
            item.file_path,
        )
        task.dataset_item_id = item.id
        members.append(task)
    await db_session.flush()
    return SimpleNamespace(
        db=db_session, actor=actor, project=project, backend=backend, tasks=members
    )


async def freeze(world, *, proof=True, unit="video"):
    snapshot = await create_snapshot(
        world.db,
        world.project.id,
        {
            "stages": [
                {
                    "stage": 7,
                    "parent_stage": None,
                    "ml_backend_id": str(world.backend.id),
                    "model_id": "track",
                    "task": "tracker",
                }
            ],
            "prompt": None,
            "output_mode": "mask",
            "execution_scope": "bulk",
            "execution_unit": unit,
            "predict_mode": "skip_predicted",
            "box_threshold": 0.3,
            "text_threshold": 0.25,
        },
    )
    frozen = None
    if proof:
        token = create_access_token(subject=str(world.actor.id), role=world.actor.role)
        frozen = await references.capture_authority(
            world.db,
            world.project,
            world.actor,
            SimpleNamespace(
                state=SimpleNamespace(api_key_id=None, api_key_scopes=None),
                headers={"authorization": "Bearer " + token},
            ),
            world.tasks,
            snapshot,
            scope="bulk",
            batch_id=None,
            predict_mode="skip_predicted",
            execution_unit=unit,
        )
    ids = [str(task.id) for task in world.tasks]
    job = await jobs.create_job(
        world.db,
        kind="batch_predict",
        user_id=world.actor.id,
        project_id=world.project.id,
        payload={
            "scope": {"task_ids": ids},
            "task_ids": ids,
            **({"video_prediction_authority": frozen} if frozen is not None else {}),
        },
    )
    job.execution_snapshot_id, job.celery_task_id = snapshot.id, str(job.id)
    await world.db.commit()
    return job


async def run_selection(world, monkeypatch, job, *, mutate=None):
    """Observe real selected rows, then stop before unrelated media execution."""
    import sqlalchemy.ext.asyncio as sa_async

    selected = []
    execute = world.db.execute
    invoked = False

    async def observe(statement, *args, **kwargs):
        nonlocal invoked
        entities = [
            column.get("entity")
            for column in getattr(statement, "column_descriptions", [])
        ]
        if getattr(statement, "is_select", False) and entities == [Task]:
            assert not invoked, "Only the final one-entity Task selection is observed"
            invoked = True
            if mutate is not None:
                await mutate(world.db)
            result = await execute(statement, *args, **kwargs)
            frozen = result.freeze()
            selected.extend(row.id for row in frozen().scalars())
            return frozen()
        return await execute(statement, *args, **kwargs)

    @asynccontextmanager
    async def session():
        yield world.db

    def progress(*_args, **kwargs):
        assert kwargs.get("status") == "running"
        raise _SelectionReached()

    monkeypatch.setattr(world.db, "execute", observe)
    monkeypatch.setattr(
        sa_async, "create_async_engine", lambda *a, **kw: SimpleNamespace()
    )
    monkeypatch.setattr(sa_async, "async_sessionmaker", lambda *a, **kw: session)
    monkeypatch.setattr(worker, "_publish_progress", progress)
    try:
        await worker._run_batch(
            str(world.project.id),
            str(world.backend.id),
            [str(task.id) for task in world.tasks],
            user_id=str(world.actor.id),
            async_job_id=str(job.id),
            celery_task_id=str(job.id),
            worker_node_id="worker:frozen-video-scope",
            predict_mode="skip_predicted",
        )
    finally:
        world.selected = selected


@pytest.mark.parametrize("changed", [1, 2])
async def test_frozen_video_keeps_original_scope_when_some_or_all_counts_change(
    world, monkeypatch, changed
):
    job = await freeze(world)
    saved = deepcopy(job.payload["video_prediction_authority"])
    for task in world.tasks[:changed]:
        task.total_predictions = 1
    await world.db.commit()
    with pytest.raises(_SelectionReached):
        await run_selection(world, monkeypatch, job)
    assert set(world.selected) == {task.id for task in world.tasks}
    persisted = await world.db.get(AsyncJob, job.id, populate_existing=True)
    assert persisted.status == "running"
    assert persisted.payload["total_tasks"] == 2
    assert persisted.payload["video_prediction_authority"] == saved


@pytest.mark.parametrize("change", ["status", "delete"])
async def test_frozen_video_rejects_mid_admission_missing_or_ineligible_task(
    world, monkeypatch, change
):
    job = await freeze(world)
    target = world.tasks[0].id

    async def mutate(db):
        statement = (
            delete(Task).where(Task.id == target)
            if change == "delete"
            else update(Task).where(Task.id == target).values(status="in_progress")
        )
        await db.execute(statement.execution_options(synchronize_session=False))

    with pytest.raises(ValueError, match="Frozen prediction task scope changed"):
        await run_selection(world, monkeypatch, job, mutate=mutate)
    assert set(world.selected) == {world.tasks[1].id}
    assert "total_tasks" not in job.payload


async def test_legacy_whole_video_without_authority_still_refuses_before_selection(
    world, monkeypatch
):
    job = await freeze(world, proof=False)
    with pytest.raises(ContentError, match="prediction_video_requires_new_request"):
        await run_selection(world, monkeypatch, job)
    assert world.selected == []


@pytest.mark.parametrize("changed", [1, 2])
async def test_legacy_frame_fanout_keeps_its_own_skip_predicted_selection(
    world, monkeypatch, changed
):
    job = await freeze(world, proof=False, unit="frame")
    for task in world.tasks[:changed]:
        task.total_predictions = 1
    await world.db.commit()
    with pytest.raises(_SelectionReached):
        await run_selection(world, monkeypatch, job)
    assert set(world.selected) == {task.id for task in world.tasks[changed:]}
    assert job.payload["total_tasks"] == 2 - changed
