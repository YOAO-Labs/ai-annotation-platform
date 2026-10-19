"""Native image-manifest scope for Scene-mode image Tasks."""

from __future__ import annotations

from uuid import uuid4

import pytest

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
