"""REST retries preserve unlink identity across a fresh association generation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.asset_storage_deletion import AssetStorageDeletion
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_unlink_operation import DatasetUnlinkOperation
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.task import Task
from app.services.storage import storage_service
from tests.factory import create_project

pytestmark = pytest.mark.asyncio


async def _seed(db, owner_id):
    suffix = uuid.uuid4().hex[:12]
    project = await create_project(db, owner_id=owner_id, name=f"unlink-{suffix}")
    dataset = Dataset(
        display_id=f"DS-HTTP-{suffix}",
        name=f"unlink-http-{suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.jpg",
        file_path=f"sources/{suffix}/source.jpg",
        file_type="image",
    )
    db.add(item)
    await db.flush()
    association, task = await _associate(db, dataset.id, project.id, item.id)
    asset = ImagePyramidAsset(task_id=task.id, profile_version="unlink-http")
    db.add(asset)
    await db.flush()
    return dataset.id, project.id, item.id, association.id, task.id, asset.id


async def _associate(db, dataset_id, project_id, item_id):
    association = ProjectDataset(dataset_id=dataset_id, project_id=project_id)
    task = Task(
        project_id=project_id,
        dataset_item_id=item_id,
        display_id=f"T-HTTP-{uuid.uuid4().hex[:12]}",
        file_name="source.jpg",
        file_path="retained-source.jpg",
        file_type="image",
        status="pending",
    )
    db.add_all([association, task])
    await db.flush()
    return association, task


def _headers(token, operation_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if operation_id is not None:
        headers["Idempotency-Key"] = str(operation_id)
    return headers


async def test_lost_response_replay_does_not_unlink_a_new_generation(
    httpx_client, db_session, super_admin, media_dispatcher, monkeypatch
):
    user, token = super_admin
    dataset_id, project_id, item_id, old_association, old_task, asset_id = await _seed(
        db_session, user.id
    )
    operation_id = uuid.uuid4()
    path = f"/api/v1/datasets/{dataset_id}/link/{project_id}"

    def no_object_io(*args, **kwargs):
        pytest.fail("REST unlink must leave object IO to the committed cleanup worker")

    monkeypatch.setattr(storage_service, "delete_prefix", no_object_io)
    response = await httpx_client.delete(
        path,
        headers={
            **_headers(token, operation_id),
            "Origin": settings.cors_allow_origins[0],
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["Idempotency-Key"] == str(operation_id)
    assert (
        "idempotency-key" in response.headers["access-control-expose-headers"].lower()
    )
    original = response.json()
    assert original == {
        "deleted_tasks": 1,
        "deleted_annotations": 0,
        "deleted_batches": 0,
        "deleted_batch_ids": [],
        "soft": False,
    }
    assert await db_session.scalar(select(Task.id).where(Task.id == old_task)) is None
    assert (
        await db_session.scalar(
            select(ProjectDataset.id).where(ProjectDataset.id == old_association)
        )
        is None
    )
    pending = (
        await db_session.execute(
            select(
                AssetStorageDeletion.asset_id, AssetStorageDeletion.completed_at
            ).where(AssetStorageDeletion.operation_id == operation_id)
        )
    ).one()
    assert pending == (asset_id, None)

    new_association, new_task = await _associate(
        db_session, dataset_id, project_id, item_id
    )
    association_id, task_id = new_association.id, new_task.id
    await db_session.commit()
    replay = await httpx_client.delete(path, headers=_headers(token, operation_id))
    assert replay.status_code == 200, replay.text
    assert replay.json() == original
    assert replay.headers["Idempotency-Key"] == str(operation_id)
    assert await db_session.scalar(select(Task.id).where(Task.id == task_id)) == task_id
    assert (
        await db_session.scalar(
            select(ProjectDataset.id).where(ProjectDataset.id == association_id)
        )
        == association_id
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetUnlinkOperation)
            .where(DatasetUnlinkOperation.operation_id == operation_id)
        )
        == 1
    )
    assert media_dispatcher.asset_storage_cleanups == [operation_id, operation_id]

    other = await create_project(db_session, owner_id=user.id, name="other scope")
    conflict = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}/link/{other.id}",
        headers=_headers(token, operation_id),
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == {"reason": "operation_conflict"}
    assert media_dispatcher.asset_storage_cleanups == [operation_id, operation_id]


async def test_broker_failure_after_commit_leaves_durable_cleanup(
    httpx_client, db_session, super_admin, media_dispatcher, monkeypatch
):
    user, token = super_admin
    dataset_id, project_id, _, _, _, asset_id = await _seed(db_session, user.id)
    dispatched = []

    def unavailable(operation_id):
        assert not db_session.in_transaction()
        dispatched.append(operation_id)
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(media_dispatcher, "dispatch_asset_storage_cleanup", unavailable)
    response = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}/link/{project_id}", headers=_headers(token)
    )
    assert response.status_code == 200, response.text
    operation_id = uuid.UUID(response.headers["Idempotency-Key"])
    assert dispatched == [operation_id]
    assert (
        await db_session.scalar(
            select(DatasetUnlinkOperation.operation_id).where(
                DatasetUnlinkOperation.operation_id == operation_id
            )
        )
        == operation_id
    )
    assert (
        await db_session.scalar(
            select(AssetStorageDeletion.asset_id).where(
                AssetStorageDeletion.operation_id == operation_id,
                AssetStorageDeletion.completed_at.is_(None),
            )
        )
        == asset_id
    )


async def test_commit_failure_never_dispatches_cleanup(
    httpx_client, db_session, super_admin, media_dispatcher, monkeypatch
):
    user, token = super_admin
    dataset_id, project_id, *_ = await _seed(db_session, user.id)
    monkeypatch.setattr(
        db_session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        await httpx_client.delete(
            f"/api/v1/datasets/{dataset_id}/link/{project_id}", headers=_headers(token)
        )
    assert media_dispatcher.asset_storage_cleanups == []


async def test_invalid_idempotency_header_cannot_mutate_or_dispatch(
    httpx_client, db_session, super_admin, media_dispatcher
):
    user, token = super_admin
    dataset_id, project_id, _, association_id, task_id, _ = await _seed(
        db_session, user.id
    )
    response = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}/link/{project_id}",
        headers=_headers(token, "invalid-key"),
    )
    assert response.status_code == 422, response.text
    assert media_dispatcher.asset_storage_cleanups == []
    assert await db_session.scalar(select(Task.id).where(Task.id == task_id)) == task_id
    assert (
        await db_session.scalar(
            select(ProjectDataset.id).where(ProjectDataset.id == association_id)
        )
        == association_id
    )
