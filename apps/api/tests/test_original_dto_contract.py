"""Business metadata stays available without claiming an original is readable."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.api.v1.tasks._shared import _attach_image_pyramids_batch, _task_with_url
from app.db.models.dataset import Dataset, DatasetItem
from app.services import original_binary, original_sources
from app.services.content_snapshot import ContentError
from app.services.dataset import DatasetService
from app.services.storage import storage_service
from tests.factory import create_project, create_task


def _no_original_signing(*args, **kwargs):
    pytest.fail("DTO serialization must not sign original bytes or claim verification")


async def _unknown_original(*args, **kwargs):
    # Unknown/legacy media has no verified ownership facts; saved-owner poster
    # lookups must degrade to no URL instead of signing or guessing.
    raise ContentError("content_unverified")


@pytest.fixture
def lazy_only(monkeypatch):
    # Saved-owner read models may resolve bounded ownership facts (for example a
    # video poster's saved owner), but serialization must never presign original
    # bytes or mint a freshness/verification claim.
    monkeypatch.setattr(storage_service, "generate_download_url", _no_original_signing)
    monkeypatch.setattr(original_binary, "public_identity", _no_original_signing)
    monkeypatch.setattr(original_sources, "resolve_task", _unknown_original)
    monkeypatch.setattr(original_sources, "resolve_item", _unknown_original)


@pytest.mark.parametrize("surface", ["list", "detail", "next", "data_manager"])
@pytest.mark.parametrize("file_type", ["image", "video"])
async def test_unknown_task_metadata_uses_lazy_identity_in_every_serializer_caller(
    db_session, httpx_client, super_admin, lazy_only, surface, file_type
):
    actor, token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    task.file_path = f"unknown-legacy/{file_type}"
    task.file_type = file_type
    await db_session.flush()
    await db_session.refresh(task, ["updated_at"])
    project_id, task_id = project.id, task.id
    headers = {"Authorization": f"Bearer {token}"}
    if surface == "data_manager":
        response = await httpx_client.post(
            f"/api/v1/projects/{project_id}/tasks/query", headers=headers, json={}
        )
    else:
        url = {
            "list": f"/api/v1/tasks?project_id={project_id}",
            "detail": f"/api/v1/tasks/{task_id}",
            "next": f"/api/v1/tasks/next?project_id={project_id}",
        }[surface]
        response = await httpx_client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    item = body["items"][0] if surface in {"list", "data_manager"} else body
    assert item["id"] == str(task_id)
    assert item["file_url"] is None
    assert item["original_identity_path"] == (
        f"/api/v1/files/tasks/{task_id}/original-identity"
    )
    assert item["content_sha256"] is None
    assert "verification_state" not in item


@pytest.mark.parametrize("surface", ["list", "detail"])
@pytest.mark.parametrize("file_type", ["image", "video"])
async def test_unknown_item_remains_unverified_metadata_with_lazy_identity(
    db_session, httpx_client, super_admin, lazy_only, surface, file_type
):
    actor, token = super_admin
    dataset = Dataset(
        display_id=f"DTO-{uuid.uuid4().hex[:8]}", name="Legacy", created_by=actor.id
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"legacy.{file_type}",
        file_path=f"unknown-legacy/item.{file_type}",
        file_type=file_type,
        content_hash="1" * 32,
    )
    db_session.add(item)
    await db_session.flush()
    dataset_id, item_id = dataset.id, item.id
    base = f"/api/v1/datasets/{dataset_id}/items"
    response = await httpx_client.get(
        base if surface == "list" else f"{base}/{item_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    item = body["items"][0] if surface == "list" else body
    assert item["id"] == str(item_id) and item["file_url"] is None
    assert item["original_identity_path"] == f"{base}/{item_id}/original-identity"
    assert item["thumbnail_url"] is None
    assert item["availability"]["state"] == "unverified"
    assert "sha256" not in item and "verification_state" not in item


@pytest.mark.parametrize("dataset_bound", [False, True])
async def test_video_dto_uses_exact_lazy_identity_without_signing(
    db_session, super_admin, lazy_only, dataset_bound
):
    actor = super_admin[0]
    project = await create_project(db_session, owner_id=actor.id)
    dataset = Dataset(
        display_id=f"VID-{uuid.uuid4().hex[:8]}", name="Video", created_by=actor.id
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="legacy.mp4",
        file_path="video/original.mp4",
        file_type="video",
    )
    db_session.add(item)
    task = await create_task(db_session, project_id=project.id)
    await db_session.flush()
    task.file_type = "video"
    task.file_path = item.file_path
    task.dataset_item_id = item.id if dataset_bound else None
    await db_session.flush()
    await db_session.refresh(task, ["updated_at"])
    task_dto = _task_with_url(task)
    item_dtos, total = await DatasetService(db_session).list_items(dataset.id)
    assert total == 1
    assert task_dto.file_url is None and item_dtos[0]["file_url"] is None
    assert task_dto.thumbnail_url is None and item_dtos[0]["thumbnail_url"] is None
    assert task_dto.original_identity_path == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
    assert item_dtos[0]["original_identity_path"] == (
        f"/api/v1/datasets/{dataset.id}/items/{item.id}/original-identity"
    )


async def test_reference_video_dto_does_not_advertise_managed_playback(
    db_session, super_admin, lazy_only
):
    from app.services.reference_keys import reference_key

    project = await create_project(db_session, owner_id=super_admin[0].id)
    task = await create_task(db_session, project_id=project.id)
    task.file_type = "video"
    task.file_path = reference_key(uuid.uuid4(), "a" * 64)
    await db_session.flush()
    await db_session.refresh(task, ["updated_at"])

    dto = _task_with_url(task)
    assert dto.storage_mode == "reference"
    assert dto.file_url is None and dto.original_identity_path is None
    assert dto.content_sha256 == "a" * 64


async def test_image_thumbnail_signing_remains_separate_from_original_identity(
    db_session, super_admin, monkeypatch
):
    from app.services import image_pyramid, thumbnail_outputs

    project = await create_project(db_session, owner_id=super_admin[0].id)
    task = await create_task(db_session, project_id=project.id)
    path = "thumbnails/derived.webp"
    task.thumbnail_path = path
    await db_session.flush()
    signed = []

    def sign(key, **kwargs):
        signed.append((key, kwargs["bucket"]))
        return "https://storage.invalid/thumbnail"

    async def saved_pointer(db, owner_kind, owner_id, requested):
        # The saved business pointer names this task's derived thumbnail.
        assert (owner_kind, owner_id, requested) == ("task", task.id, path)
        return "task_thumbnail", task.id, None

    monkeypatch.setattr(storage_service, "generate_download_url", sign)
    monkeypatch.setattr(thumbnail_outputs, "_business_pointer", saved_pointer)
    # One isolated test session cannot be reopened; ownership lookup is covered
    # by the database seam, this test covers the saved-owner signing contract.
    monkeypatch.setattr(
        image_pyramid, "task_pyramid_assets", AsyncMock(return_value={})
    )
    await _attach_image_pyramids_batch(
        db_session, [task], {task.id: (None, None, path, None, None)}
    )
    assert signed == [(path, storage_service.media_cache_bucket)]
    assert task._media_cache_thumbnail == (path, "https://storage.invalid/thumbnail")
    signed.clear()

    dto = _task_with_url(task, thumbnail_path=path)
    assert dto.file_url is None and dto.original_identity_path
    assert dto.thumbnail_url == "https://storage.invalid/thumbnail"
    assert signed == []
