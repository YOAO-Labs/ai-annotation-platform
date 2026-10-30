"""Owner-backed training finalization retains its snapshot row fences."""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import training_datasets as service
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.factory import create_user
from tests.sample_evaluation_original_fixtures import Objects
from tests.test_training_datasets import reviewed_project


@pytest.mark.parametrize("locked_row", ["task", "annotation"])
async def test_finalization_fails_closed_on_locked_snapshot_rows(
    test_engine, monkeypatch, locked_row
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    objects = Objects().install(monkeypatch)
    async with maker.begin() as db:
        owner = await create_user(
            db,
            "project_admin",
            f"training-fence-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project, tasks = await reviewed_project(db, owner, original_objects=objects)
        project_id, owner_id = project.id, owner.id
        item_ids = [task.dataset_item_id for task in tasks]
        dataset_ids = list(
            await db.scalars(
                select(DatasetItem.dataset_id).where(DatasetItem.id.in_(item_ids))
            )
        )
    try:
        async with maker() as db:
            prepared = await service.prepare_selection(
                db,
                project,
                TrainingDatasetSelection(),
                actor_id=owner_id,
                trusted_actor=True,
            )
            assert prepared.report["ready"]
            original_reads = list(objects.calls)
            task_id = uuid.UUID(prepared.snapshot["samples"][0]["task_id"])
            async with maker() as editor:
                if locked_row == "task":
                    statement = select(Task.id).where(Task.id == task_id)
                else:
                    statement = select(Annotation.id).where(
                        Annotation.task_id == task_id
                    )
                assert (await editor.execute(statement.with_for_update())).first()
                with pytest.raises(ContentError) as failure:
                    await service.finalize_selection(db, prepared)
                assert failure.value.code == "storage_original_busy"
                await editor.rollback()
            snapshot, report = await service.finalize_selection(db, prepared)
            assert snapshot == prepared.snapshot
            assert report["digest"] == prepared.report["digest"]
            await db.rollback()
            async with maker.begin() as editor:
                annotation = await editor.scalar(
                    select(Annotation).where(Annotation.task_id == task_id)
                )
                annotation.geometry = {
                    "type": "bbox",
                    "x": 0.2,
                    "y": 0.2,
                    "w": 0.1,
                    "h": 0.1,
                }
                annotation.version += 1
            with pytest.raises(TrainingDatasetError) as changed:
                await service.finalize_selection(db, prepared)
            assert changed.value.code == "preflight_changed"
            assert objects.calls == original_reads
    finally:
        async with maker.begin() as db:
            await db.execute(
                delete(Annotation).where(Annotation.project_id == project_id)
            )
            await db.execute(delete(Task).where(Task.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
            await db.execute(delete(User).where(User.id == owner_id))
        assert all(body.closed for body in objects.bodies)
