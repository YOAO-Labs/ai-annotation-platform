"""Behavior tests for the narrow Workspace configuration dependencies."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.ml_backend_pool import (
    MLBackendPoolMember,
    MLBackendServicePool,
)
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.ml_backend import MLBackendService
from tests.factory import create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


async def test_backend_enablement_keeps_legacy_write_order_and_caller_rollback(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_ids = []
    pool_ids = []
    async with maker() as session:
        user = await create_user(
            session,
            "project_admin",
            f"config-order-{uuid.uuid4()}@example.test",
            "Configuration order",
        )
        service = MLBackendService(session)
        registries = []
        for label in ("legacy", "native"):
            project = await create_project(session, owner_id=user.id, name=label)
            registry = MLBackendRegistry(
                name=label,
                url=f"https://{label}.example.test",
                state="connected",
                auth_method="none",
            )
            session.add(registry)
            await session.flush()
            pool = await service._create_singleton_pool(registry)
            project_ids.append(project.id)
            pool_ids.append(pool.id)
            registries.append(registry.id)

        await session.execute(
            text("CREATE TEMP TABLE cfg_enable_order (position bigserial, event text)")
        )
        await session.execute(
            text(
                "CREATE FUNCTION pg_temp.cfg_enable_order() RETURNS trigger "
                "LANGUAGE plpgsql SECURITY DEFINER "
                "SET search_path=pg_catalog,pg_temp AS $$ BEGIN "
                "INSERT INTO pg_temp.cfg_enable_order(event) "
                "VALUES (TG_TABLE_NAME || ':' || TG_OP); RETURN NEW; END $$"
            )
        )
        for table, operations in (
            ("ml_backend_service_pools", "UPDATE"),
            ("project_ml_backend_pool", "INSERT OR UPDATE"),
        ):
            await session.execute(
                text(
                    f"CREATE TRIGGER cfg_enable_order AFTER {operations} "
                    f"ON public.{table} FOR EACH ROW "
                    "EXECUTE FUNCTION pg_temp.cfg_enable_order()"
                )
            )

        await service.set_enabled(project_ids[0], registries[0], enabled=True)
        legacy_order = list(
            await session.scalars(
                text("SELECT event FROM pg_temp.cfg_enable_order ORDER BY position")
            )
        )
        assert legacy_order == [
            "ml_backend_service_pools:UPDATE",
            "project_ml_backend_pool:INSERT",
        ]
        await session.execute(text("TRUNCATE pg_temp.cfg_enable_order"))
        native = await _call(
            session,
            "SELECT aap_inference_v1.enable_workspace_project_backend"
            "(CAST(:project AS uuid), CAST(:registry AS uuid))",
            {"project": project_ids[1], "registry": registries[1]},
        )
        assert native["pool_id"] == str(pool_ids[1])
        assert (
            list(
                await session.scalars(
                    text("SELECT event FROM pg_temp.cfg_enable_order ORDER BY position")
                )
            )
            == legacy_order
        )
        assert session.in_transaction()
        await session.rollback()

    async with maker() as observer:
        assert await observer.get(Project, project_ids[1]) is None
        assert await observer.get(MLBackendServicePool, pool_ids[1]) is None
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM public.project_ml_backend_pool WHERE project_id=:project"
                ),
                {"project": project_ids[1]},
            )
            == 0
        )


def _decoded_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _call(session, function: str, params: dict):
    result = await session.execute(
        text(function),
        params,
    )
    return _decoded_json(result.scalar_one())


async def test_identity_org_scope_ignores_role_and_excludes_soft_deleted_memberships(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    actor_id = None
    organization_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            actor = await create_user(
                session,
                "super_admin",
                f"workspace-config-org-{suffix}@example.test",
                "Workspace configuration actor",
            )
            actor_id = actor.id
            visible_org = Organization(
                name=f"Visible {suffix}",
                slug=f"workspace-config-visible-{suffix}",
                created_by=actor.id,
            )
            hidden_org = Organization(
                name=f"Hidden {suffix}",
                slug=f"workspace-config-hidden-{suffix}",
                created_by=actor.id,
            )
            session.add_all([visible_org, hidden_org])
            await session.flush()
            organization_ids = [visible_org.id, hidden_org.id]
            session.add_all(
                [
                    OrganizationMember(
                        organization_id=visible_org.id,
                        user_id=actor.id,
                        role="unrecognized-but-visible",
                    ),
                    OrganizationMember(
                        organization_id=hidden_org.id,
                        user_id=actor.id,
                        role="member",
                        deleted_at=datetime.now(timezone.utc),
                    ),
                ]
            )
            await session.flush()

            visible = await _call(
                session,
                """
                SELECT aap_identity_v1.workspace_configuration_org_scope(
                    CAST(:actor AS uuid), CAST(:organization AS uuid)
                )
                """,
                {"actor": actor.id, "organization": visible_org.id},
            )
            hidden = await _call(
                session,
                """
                SELECT aap_identity_v1.workspace_configuration_org_scope(
                    CAST(:actor AS uuid), CAST(:organization AS uuid)
                )
                """,
                {"actor": actor.id, "organization": hidden_org.id},
            )

        assert visible == {
            "schema": "aap-identity-workspace-configuration/v1",
            "actor_id": str(actor_id),
            "organization_id": str(organization_ids[0]),
            "visible": True,
        }
        assert hidden == {
            "schema": "aap-identity-workspace-configuration/v1",
            "actor_id": str(actor_id),
            "organization_id": str(organization_ids[1]),
            "visible": False,
        }
    finally:
        if actor_id is not None:
            async with maker.begin() as session:
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id == actor_id
                    )
                )
                if organization_ids:
                    await session.execute(
                        delete(Organization).where(
                            Organization.id.in_(organization_ids)
                        )
                    )
                await session.execute(delete(User).where(User.id == actor_id))


async def test_assets_dataset_facts_return_only_linked_datasets_and_scene_presence(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-config-assets-{suffix}@example.test",
                "Workspace assets actor",
            )
            user_id = user.id
            project = await create_project(
                session, owner_id=user.id, name=f"Workspace config {suffix}"
            )
            other_project = await create_project(
                session, owner_id=user.id, name=f"Other workspace config {suffix}"
            )
            project_ids = [project.id, other_project.id]
            linked_with_scenes = Dataset(
                display_id=f"DSC-{suffix[:8]}",
                name=f"Linked with scenes {suffix}",
                data_type="video",
                created_by=user.id,
            )
            linked_without_scenes = Dataset(
                display_id=f"DSN-{suffix[:8]}",
                name=f"Linked without scenes {suffix}",
                data_type="image",
                created_by=user.id,
            )
            only_other_project = Dataset(
                display_id=f"DSO-{suffix[:8]}",
                name=f"Other project only {suffix}",
                data_type="lidar",
                created_by=user.id,
            )
            session.add_all(
                [linked_with_scenes, linked_without_scenes, only_other_project]
            )
            await session.flush()
            dataset_ids = [
                linked_with_scenes.id,
                linked_without_scenes.id,
                only_other_project.id,
            ]
            session.add_all(
                [
                    ProjectDataset(
                        project_id=project.id, dataset_id=linked_with_scenes.id
                    ),
                    ProjectDataset(
                        project_id=project.id, dataset_id=linked_without_scenes.id
                    ),
                    ProjectDataset(
                        project_id=other_project.id, dataset_id=only_other_project.id
                    ),
                    Scene(
                        display_id=f"SCN-{suffix[:8]}",
                        dataset_id=linked_with_scenes.id,
                        name=f"Scene {suffix}",
                        created_by=user.id,
                    ),
                ]
            )
            await session.flush()

            facts = await _call(
                session,
                """
                SELECT aap_assets_v1.workspace_configuration_datasets(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )

        assert facts["schema"] == "aap-assets-workspace-configuration/v1"
        assert facts["project_id"] == str(project_ids[0])
        assert {
            row["id"]: (row["display_id"], row["data_type"], row["has_scenes"])
            for row in facts["datasets"]
        } == {
            str(dataset_ids[0]): (linked_with_scenes.display_id, "video", True),
            str(dataset_ids[1]): (
                linked_without_scenes.display_id,
                "image",
                False,
            ),
        }
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
                        delete(Dataset).where(Dataset.id.in_(dataset_ids))
                    )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
                if user_id is not None:
                    await session.execute(delete(User).where(User.id == user_id))


async def test_labeling_video_facts_count_active_blockers_and_distinct_items(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    dataset_id = None
    item_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-config-labeling-{suffix}@example.test",
                "Workspace labeling actor",
            )
            user_id = user.id
            project = await create_project(
                session, owner_id=user.id, name=f"Video project {suffix}"
            )
            other_project = await create_project(
                session, owner_id=user.id, name=f"Other video project {suffix}"
            )
            project_ids = [project.id, other_project.id]
            dataset = Dataset(
                display_id=f"DL-{suffix[:8]}",
                name=f"Video dataset {suffix}",
                data_type="video",
                created_by=user.id,
            )
            session.add(dataset)
            await session.flush()
            dataset_id = dataset.id
            items = [
                DatasetItem(
                    dataset_id=dataset.id,
                    file_name=f"{suffix}-{index}.mp4",
                    file_path=f"videos/{suffix}-{index}.mp4",
                    file_type="video",
                )
                for index in range(2)
            ]
            session.add_all(items)
            await session.flush()
            item_ids = [item.id for item in items]

            first = await create_task(session, project_id=project.id)
            duplicate_item = await create_task(session, project_id=project.id)
            no_item = await create_task(session, project_id=project.id)
            foreign = await create_task(session, project_id=other_project.id)
            first.dataset_item_id = item_ids[0]
            duplicate_item.dataset_item_id = item_ids[0]
            foreign.dataset_item_id = item_ids[1]
            task_ids = [first.id, duplicate_item.id, no_item.id, foreign.id]
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
                    Annotation(
                        task_id=first.id,
                        project_id=project.id,
                        user_id=user.id,
                        annotation_type="bbox",
                        class_name="car",
                        geometry={"type": "bbox", "x": 1, "y": 2, "w": 3, "h": 4},
                        is_active=True,
                        was_cancelled=True,
                    ),
                ]
            )
            session.add_all(
                [
                    VideoTrackerJob(
                        task_id=first.id,
                        dataset_item_id=item_ids[0],
                        status=status,
                        model_key=f"workspace-config-{status}",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={},
                        event_channel=f"workspace-config:{suffix}:{status}",
                    )
                    for status in ("queued", "running", "completed")
                ]
                + [
                    VideoTrackerJob(
                        task_id=foreign.id,
                        dataset_item_id=item_ids[1],
                        status="running",
                        model_key="workspace-config-foreign",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={},
                        event_channel=f"workspace-config:{suffix}:foreign",
                    )
                ]
            )
            await session.flush()

            facts = await _call(
                session,
                """
                SELECT aap_labeling_v1.workspace_configuration_video_facts(
                    CAST(:project AS uuid)
                )
                """,
                {"project": project.id},
            )

        assert facts == {
            "schema": "aap-labeling-workspace-configuration/v1",
            "project_id": str(project_ids[0]),
            "annotation_count": 1,
            "active_tracker_count": 2,
            "dataset_item_ids": [str(item_ids[0])],
        }
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


async def test_inference_backend_enablement_matches_legacy_noop_and_missing_pool(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_id = None
    registry_ids: list[uuid.UUID] = []
    pool_id = None
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-config-inference-{suffix}@example.test",
                "Workspace inference actor",
            )
            user_id = user.id
            project = await create_project(
                session, owner_id=user.id, name=f"Inference project {suffix}"
            )
            project_id = project.id
            registry = MLBackendRegistry(
                name=f"Workspace configuration {suffix}",
                url=f"https://workspace-config-{suffix}.example.test",
                state="connected",
                auth_method="none",
                extra_params={},
                source="manual",
            )
            unpooled_registry = MLBackendRegistry(
                name=f"Workspace configuration unpooled {suffix}",
                url=f"https://workspace-config-unpooled-{suffix}.example.test",
                state="connected",
                auth_method="none",
                extra_params={},
                source="manual",
            )
            session.add_all([registry, unpooled_registry])
            await session.flush()
            registry_ids = [registry.id, unpooled_registry.id]
            pool = MLBackendServicePool(
                name=f"Workspace pool {suffix}",
                enabled=False,
                legacy_instance_id=registry.id,
            )
            session.add(pool)
            await session.flush()
            pool_id = pool.id
            session.add(
                MLBackendPoolMember(
                    pool_id=pool.id,
                    registry_id=registry.id,
                    traffic_state="active",
                    weight=1,
                )
            )
            await session.flush()

        async with maker.begin() as session:
            first = await _call(
                session,
                """
                SELECT aap_inference_v1.enable_workspace_project_backend(
                    CAST(:project AS uuid), CAST(:registry AS uuid)
                )
                """,
                {"project": project_id, "registry": registry_ids[0]},
            )
        assert first == {
            "schema": "aap-inference-workspace-configuration/v1",
            "project_id": str(project_id),
            "registry_id": str(registry_ids[0]),
            "pool_id": str(pool_id),
        }

        async with maker() as session:
            first_state = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT pool.enabled, pool.routing_generation, pool.updated_at,
                               association.enabled AS association_enabled,
                               association.updated_at AS association_updated_at
                        FROM public.ml_backend_service_pools AS pool
                        JOIN public.project_ml_backend_pool AS association
                          ON association.pool_id = pool.id
                        WHERE pool.id = CAST(:pool AS uuid)
                          AND association.project_id = CAST(:project AS uuid)
                        """
                        ),
                        {"pool": pool_id, "project": project_id},
                    )
                )
                .mappings()
                .one()
            )
        assert first_state["enabled"] is True
        assert first_state["association_enabled"] is True
        first_generation = first_state["routing_generation"]

        await asyncio.sleep(0.02)
        async with maker.begin() as session:
            second = await _call(
                session,
                """
                SELECT aap_inference_v1.enable_workspace_project_backend(
                    CAST(:project AS uuid), CAST(:registry AS uuid)
                )
                """,
                {"project": project_id, "registry": registry_ids[0]},
            )
            missing_pool = await _call(
                session,
                """
                SELECT aap_inference_v1.enable_workspace_project_backend(
                    CAST(:project AS uuid), CAST(:registry AS uuid)
                )
                """,
                {"project": project_id, "registry": registry_ids[1]},
            )
        assert second == first
        assert missing_pool == {
            "schema": "aap-inference-workspace-configuration/v1",
            "project_id": str(project_id),
            "registry_id": str(registry_ids[1]),
            "pool_id": None,
        }

        async with maker() as session:
            second_state = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT pool.enabled, pool.routing_generation, pool.updated_at,
                               association.enabled AS association_enabled,
                               association.updated_at AS association_updated_at
                        FROM public.ml_backend_service_pools AS pool
                        JOIN public.project_ml_backend_pool AS association
                          ON association.pool_id = pool.id
                        WHERE pool.id = CAST(:pool AS uuid)
                          AND association.project_id = CAST(:project AS uuid)
                        """
                        ),
                        {"pool": pool_id, "project": project_id},
                    )
                )
                .mappings()
                .one()
            )
            association_count = await session.scalar(
                text(
                    """
                    SELECT count(*) FROM public.project_ml_backend_pool
                    WHERE project_id = CAST(:project AS uuid)
                    """
                ),
                {"project": project_id},
            )
        assert second_state["enabled"] is True
        assert second_state["routing_generation"] == first_generation
        assert second_state["updated_at"] == first_state["updated_at"]
        assert (
            second_state["association_updated_at"]
            == first_state["association_updated_at"]
        )
        assert association_count == 1
    finally:
        if project_id is not None:
            async with maker.begin() as session:
                await session.execute(
                    delete(ProjectMLBackendPool).where(
                        ProjectMLBackendPool.project_id == project_id
                    )
                )
                if pool_id is not None:
                    await session.execute(
                        delete(MLBackendPoolMember).where(
                            MLBackendPoolMember.pool_id == pool_id
                        )
                    )
                    await session.execute(
                        delete(MLBackendServicePool).where(
                            MLBackendServicePool.id == pool_id
                        )
                    )
                if registry_ids:
                    await session.execute(
                        delete(MLBackendRegistry).where(
                            MLBackendRegistry.id.in_(registry_ids)
                        )
                    )
                await session.execute(delete(Project).where(Project.id == project_id))
                if user_id is not None:
                    await session.execute(delete(User).where(User.id == user_id))
