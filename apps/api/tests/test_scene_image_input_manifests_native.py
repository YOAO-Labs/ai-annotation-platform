"""Native image-manifest scope for Scene-mode image Tasks."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_input_manifests import contract as manifests
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.services.accepted_input_manifests import (
    SqlAlchemyManifestDatabase,
    get_input_manifest_capture,
)
from tests.factory import create_project, create_task
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_images_execution import _seed_image
from tests.test_scene_image_input_manifests_migration import _migration

pytestmark = pytest.mark.asyncio


async def _capture_request(db, *, actor_id, project_id, task_id):
    job_id = uuid4()
    selection = manifests.ManifestSelection(
        batch_id=None, task_ids=[task_id], predict_mode="append"
    )
    job = AsyncJob(
        id=job_id,
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        status="pending",
        celery_task_id=str(job_id),
        payload={
            "batch_id": None,
            "execution_scope": "bulk",
            "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
            "accepted_input_manifest_id": str(job_id),
            "input_selection": selection.model_dump(mode="json"),
        },
        result={},
    )
    db.add(job)
    await db.flush()

    header = await get_input_manifest_capture()(db).capture(
        manifests.CaptureManifestRequest(
            job_id=job_id,
            actor_id=actor_id,
            project_id=project_id,
            selection=selection,
        )
    )
    job.payload = {
        **job.payload,
        "accepted_input_manifest_digest": header.selection_digest,
        "total_tasks": header.selected_count,
        "input_candidate_count": header.candidate_count,
    }
    await db.flush()
    return header


async def test_scene_mode_image_task_is_captured_as_its_own_image_input(db_session):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    project.scene_mode = True
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="image")
    dataset = await db_session.get(Dataset, dataset_id)
    dataset.is_temporal = True
    db_session.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
    scene_id = uuid4()
    db_session.add(
        Scene(
            id=scene_id,
            display_id=f"S-{scene_id.hex[:12]}",
            dataset_id=dataset_id,
            name="scene-input-manifest",
            created_by=actor_id,
        )
    )
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=32, height=24
    )
    item = await db_session.get(DatasetItem, item_id)
    item.scene_id = scene_id
    item.frame_index = 7
    task = await create_task(db_session, project_id=project.id)
    task.dataset_item_id = item_id
    task.file_name = item.file_name
    task.file_path = item.file_path
    task.file_type = "image"
    await db_session.flush()

    header = await _capture_request(
        db_session,
        actor_id=actor_id,
        project_id=project.id,
        task_id=task.id,
    )
    assert header.status == "preparing"
    assert header.candidate_count == header.selected_count == 1

    page = await SqlAlchemyManifestDatabase(db_session, commit=False).call(
        "read_accepted_image_input_manifest_page",
        manifests.ManifestPageRequest(
            job_id=header.job_id,
            actor_id=actor_id,
            project_id=project.id,
            selection_digest=header.selection_digest,
        ),
    )
    assert len(page.members) == 1
    member = page.members[0]
    assert member.task_id == task.id
    assert member.dataset_item_id == item_id
    assert member.file_type == "image"
    assert member.source is not None
    assert page.manifest == header
    retained_scene_manifest_count = await db_session.scalar(
        text(_migration()._UNSUPPORTED_RETAINED_MANIFEST_COUNT)
    )
    assert retained_scene_manifest_count == 1

    non_image_task = await create_task(db_session, project_id=project.id)
    non_image_task.file_type = "lidar"
    with pytest.raises(manifests.ManifestFailure, match="unsupported_media"):
        await _capture_request(
            db_session,
            actor_id=actor_id,
            project_id=project.id,
            task_id=non_image_task.id,
        )


async def test_scene_mode_lidar_project_cannot_use_an_image_manifest(db_session):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    project.data_type = "lidar"
    project.scene_mode = True
    task = await create_task(db_session, project_id=project.id)
    task.file_type = "lidar"
    await db_session.flush()

    with pytest.raises(manifests.ManifestFailure, match="unsupported_media"):
        await _capture_request(
            db_session,
            actor_id=actor_id,
            project_id=project.id,
            task_id=task.id,
        )


async def test_0227_drain_query_classifies_legacy_scene_image_jobs(db_session):
    actor_id = await _seed_actor(db_session)
    scene_project = await create_project(db_session, owner_id=actor_id)
    scene_project.scene_mode = True
    normal_project = await create_project(db_session, owner_id=actor_id)
    lidar_project = await create_project(db_session, owner_id=actor_id)
    lidar_project.data_type = "lidar"
    lidar_project.scene_mode = True
    await db_session.flush()

    def job(project, *, status="pending", payload=None):
        job_id = uuid4()
        return AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor_id,
            status=status,
            celery_task_id=str(job_id),
            payload={"batch_id": None, **(payload or {})},
            result={},
        )

    jobs = [
        job(scene_project, payload={"execution_scope": "bulk"}),
        job(
            scene_project,
            status="running",
            payload={"execution_scope": "bulk", "task": "detect"},
        ),
        *[
            job(
                scene_project,
                payload={
                    "execution_scope": "bulk",
                    "execution_unit": unit,
                },
            )
            for unit in ("scene", "frame", "video")
        ],
        *[
            job(
                scene_project,
                payload={"execution_scope": "bulk", "task": task},
            )
            for task in ("text", "tracker")
        ],
        *[
            job(
                scene_project,
                payload={
                    "execution_scope": "bulk",
                    "pipeline_stages": [{"stage": 1, "task": task}],
                },
            )
            for task in ("text", "tracker")
        ],
        *[
            job(
                scene_project,
                payload={
                    "execution_scope": "bulk",
                    "pipeline_stages": [
                        {
                            "stage": 1,
                            "source": {"kind": "dataset", "execution_unit": unit},
                        }
                    ],
                },
            )
            for unit in ("scene", "frame", "video")
        ],
        job(
            scene_project,
            payload={
                "execution_scope": "bulk",
                "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
            },
        ),
        job(scene_project, status="completed", payload={"execution_scope": "bulk"}),
        job(scene_project, payload={"execution_scope": "workbench"}),
        job(normal_project, payload={"execution_scope": "bulk"}),
        job(lidar_project, payload={"execution_scope": "bulk"}),
    ]
    db_session.add_all(jobs)
    await db_session.flush()

    active_legacy_jobs = await db_session.scalar(
        text(_migration()._LEGACY_SCENE_IMAGE_JOB_COUNT)
    )
    assert active_legacy_jobs == 2
