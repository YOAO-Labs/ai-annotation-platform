"""Native contract tests for staged Workspace configuration read dependencies."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from tests.factory import create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


def _decoded_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _call(session, query: str, params: dict):
    result = await session.execute(text(query), params)
    return _decoded_json(result.scalar_one())


async def test_assets_configuration_reads_are_scoped_and_filter_missing_items(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    item_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-config-assets-{suffix}@example.test",
                "Workspace configuration assets actor",
            )
            user_id = user.id
            project = await create_project(
                session, owner_id=user.id, name=f"Assets project {suffix}"
            )
            other_project = await create_project(
                session, owner_id=user.id, name=f"Other assets project {suffix}"
            )
            project_ids = [project.id, other_project.id]
            linked = Dataset(
                display_id=f"AS-{suffix[:8]}",
                name=f"Linked dataset {suffix}",
                data_type="video",
                created_by=user.id,
            )
            unlinked = Dataset(
                display_id=f"AU-{suffix[:8]}",
                name=f"Unlinked dataset {suffix}",
                data_type="image",
                created_by=user.id,
            )
            session.add_all([linked, unlinked])
            await session.flush()
            dataset_ids = [linked.id, unlinked.id]
            item = DatasetItem(
                dataset_id=linked.id,
                file_name=f"{suffix}.mp4",
                file_path=f"videos/{suffix}.mp4",
                file_type="video",
            )
            session.add(item)
            await session.flush()
            item_ids = [item.id]
            session.add_all(
                [
                    ProjectDataset(project_id=project.id, dataset_id=linked.id),
                    ProjectDataset(project_id=other_project.id, dataset_id=unlinked.id),
                    Scene(
                        display_id=f"SC-{suffix[:8]}",
                        dataset_id=linked.id,
                        name=f"Scene {suffix}",
                        created_by=user.id,
                    ),
                ]
            )
            await session.flush()

            rows = await _call(
                session,
                """
                SELECT aap_assets_v1.workspace_configuration_dataset_rows(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )
            linked_scenes = await _call(
                session,
                """
                SELECT aap_assets_v1.workspace_configuration_dataset_scenes(
                    CAST(:dataset AS uuid)
                )
                """,
                {"dataset": linked.id},
            )
            unlinked_scenes = await _call(
                session,
                """
                SELECT aap_assets_v1.workspace_configuration_dataset_scenes(
                    CAST(:dataset AS uuid)
                )
                """,
                {"dataset": unlinked.id},
            )
            valid_items = await _call(
                session,
                """
                SELECT aap_assets_v1.workspace_configuration_existing_item_ids(
                    CAST(:items AS uuid[])
                )
                """,
                {"items": [item.id, uuid.uuid4(), item.id]},
            )

        assert rows == {
            "schema": "aap-assets-workspace-configuration/v1",
            "project_id": str(project_ids[0]),
            "datasets": [
                {
                    "id": str(dataset_ids[0]),
                    "display_id": linked.display_id,
                    "data_type": "video",
                }
            ],
        }
        assert linked_scenes == {
            "schema": "aap-assets-workspace-configuration/v1",
            "dataset_id": str(dataset_ids[0]),
            "has_scenes": True,
        }
        assert unlinked_scenes["has_scenes"] is False
        assert valid_items["schema"] == "aap-assets-workspace-configuration/v1"
        assert valid_items["item_ids"] == [str(item_ids[0])]
    finally:
        if project_ids:
            async with maker.begin() as session:
                await session.execute(
                    delete(ProjectDataset).where(
                        ProjectDataset.project_id.in_(project_ids)
                    )
                )
                if dataset_ids:
                    await session.execute(
                        delete(Scene).where(Scene.dataset_id.in_(dataset_ids))
                    )
                    await session.execute(
                        delete(DatasetItem).where(
                            DatasetItem.dataset_id.in_(dataset_ids)
                        )
                    )
                    await session.execute(
                        delete(Dataset).where(Dataset.id.in_(dataset_ids))
                    )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
                if user_id is not None:
                    await session.execute(delete(User).where(User.id == user_id))


async def test_labeling_configuration_reads_keep_persisted_count_and_phase_facts(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    dataset_id = None
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-config-labeling-support-{suffix}@example.test",
                "Workspace configuration Labeling actor",
            )
            user_id = user.id
            project = await create_project(
                session, owner_id=user.id, name=f"Labeling project {suffix}"
            )
            other_project = await create_project(
                session, owner_id=user.id, name=f"Other Labeling project {suffix}"
            )
            project_ids = [project.id, other_project.id]
            dataset = Dataset(
                display_id=f"LB-{suffix[:8]}",
                name=f"Labeling dataset {suffix}",
                data_type="video",
                created_by=user.id,
            )
            session.add(dataset)
            await session.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name=f"{suffix}.mp4",
                file_path=f"videos/{suffix}.mp4",
                file_type="video",
            )
            session.add(item)
            await session.flush()

            first = await create_task(session, project_id=project.id)
            duplicate_item = await create_task(session, project_id=project.id)
            foreign = await create_task(session, project_id=other_project.id)
            first.dataset_item_id = item.id
            duplicate_item.dataset_item_id = item.id
            foreign.dataset_item_id = item.id
            task_ids = [first.id, duplicate_item.id, foreign.id]
            session.add_all(
                [
                    Annotation(
                        task_id=first.id,
                        project_id=project.id,
                        user_id=user.id,
                        annotation_type="bbox",
                        class_name="car",
                        geometry={"type": "bbox", "x": 1, "y": 2, "w": 3, "h": 4},
                        is_active=True,
                        was_cancelled=False,
                    ),
                    Annotation(
                        task_id=first.id,
                        project_id=project.id,
                        user_id=user.id,
                        annotation_type="bbox",
                        class_name="car",
                        geometry={"type": "bbox", "x": 1, "y": 2, "w": 3, "h": 4},
                        is_active=False,
                        was_cancelled=False,
                    ),
                    VideoTrackerJob(
                        task_id=first.id,
                        dataset_item_id=item.id,
                        status="queued",
                        model_key=f"workspace-config-{suffix}",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={},
                        event_channel=f"workspace-config:{suffix}",
                    ),
                    VideoTrackerJob(
                        task_id=first.id,
                        dataset_item_id=item.id,
                        status="completed",
                        model_key=f"workspace-config-complete-{suffix}",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={},
                        event_channel=f"workspace-config:{suffix}:complete",
                    ),
                ]
            )
            await session.flush()
            project.total_tasks = 37
            await session.flush()

            task_count = await _call(
                session,
                """
                SELECT aap_labeling_v1.workspace_configuration_task_count(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )
            annotation_count = await _call(
                session,
                """
                SELECT aap_labeling_v1.workspace_configuration_annotation_count(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )
            tracker_count = await _call(
                session,
                """
                SELECT aap_labeling_v1.workspace_configuration_tracker_count(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )
            video_items = await _call(
                session,
                """
                SELECT aap_labeling_v1.workspace_configuration_video_items(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )

        assert task_count == {
            "schema": "aap-labeling-workspace-configuration/v1",
            "project_id": str(project_ids[0]),
            "total_tasks": 37,
        }
        assert annotation_count["annotation_count"] == 1
        assert tracker_count["active_tracker_count"] == 1
        assert video_items["dataset_item_ids"] == [str(item.id)]
    finally:
        if project_ids:
            async with maker.begin() as session:
                if task_ids:
                    await session.execute(
                        delete(VideoTrackerJob).where(
                            VideoTrackerJob.task_id.in_(task_ids)
                        )
                    )
                    await session.execute(
                        delete(Annotation).where(Annotation.task_id.in_(task_ids))
                    )
                    await session.execute(delete(Task).where(Task.id.in_(task_ids)))
                if dataset_id is not None:
                    await session.execute(
                        delete(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
                    )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
                if dataset_id is not None:
                    await session.execute(
                        delete(Dataset).where(Dataset.id == dataset_id)
                    )
                if user_id is not None:
                    await session.execute(delete(User).where(User.id == user_id))


async def test_inference_configuration_reads_pool_and_opaque_registry_snapshot(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    registry_id = None
    pool_id = None
    try:
        async with maker.begin() as session:
            registry = MLBackendRegistry(
                name=f"Workspace configuration backend {suffix}",
                url=f"https://workspace-config-{suffix}.example.test",
                state="connected",
                auth_method="bearer",
                auth_token="workspace-configuration-test-token",
                extra_params={"max_concurrency": 7, "owner_test": True},
            )
            session.add(registry)
            await session.flush()
            registry_id = registry.id
            pool = MLBackendServicePool(
                name=f"Workspace configuration pool {suffix}",
                enabled=False,
                legacy_instance_id=registry.id,
            )
            session.add(pool)
            await session.flush()
            pool_id = pool.id

            source_pool = await _call(
                session,
                """
                SELECT aap_inference_v1.workspace_configuration_source_pool(
                    CAST(:pool AS uuid)
                )
                """,
                {"pool": pool.id},
            )
            missing_pool = await _call(
                session,
                """
                SELECT aap_inference_v1.workspace_configuration_source_pool(
                    CAST(:pool AS uuid)
                )
                """,
                {"pool": uuid.uuid4()},
            )
            backend = await _call(
                session,
                """
                SELECT aap_inference_v1.workspace_configuration_backend(
                    CAST(:registry AS uuid)
                )
                """,
                {"registry": registry.id},
            )
            missing_backend = await _call(
                session,
                """
                SELECT aap_inference_v1.workspace_configuration_backend(
                    CAST(:registry AS uuid)
                )
                """,
                {"registry": uuid.uuid4()},
            )

        assert source_pool == {
            "schema": "aap-inference-workspace-configuration/v1",
            "pool_id": str(pool_id),
            "legacy_registry_id": str(registry_id),
            "found": True,
        }
        assert missing_pool["found"] is False
        assert missing_pool["legacy_registry_id"] is None
        assert backend["registry_id"] == str(registry_id)
        assert backend["backend"]["id"] == str(registry_id)
        assert set(backend["backend"]) == {"id", "probe_subject"}
        subject = backend["backend"]["probe_subject"]
        assert subject.startswith("v1.")
        assert registry.url not in subject
        assert registry.auth_token not in subject
        assert missing_backend["backend"] is None
    finally:
        if pool_id is not None or registry_id is not None:
            async with maker.begin() as session:
                if pool_id is not None:
                    await session.execute(
                        delete(MLBackendServicePool).where(
                            MLBackendServicePool.id == pool_id
                        )
                    )
                if registry_id is not None:
                    await session.execute(
                        delete(MLBackendRegistry).where(
                            MLBackendRegistry.id == registry_id
                        )
                    )
