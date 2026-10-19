"""Authorization boundaries for project files and dataset scenes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from aap_assets_images.contract import (
    AcceptedImageWork,
    ImageWorkReference,
)
from app.core.security import create_access_token
from app.db.models.dataset import Dataset
from app.db.models.task import Task
from aap_assets_catalog.spatial_contract import CreateSceneRequest, GetSceneRequest
from app.services.assets_spatial import AssetsSpatialGateway
from tests.factory import create_api_key
from tests.factory import create_membership, create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


class _ImageAdmission:
    def __init__(self, *_args):
        pass

    async def admit_task(self, **_request):
        return None


def _headers(user) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


def _accepted_task_backfill(request, *, job_id: uuid.UUID) -> AcceptedImageWork:
    accepted_before = datetime.now(UTC)
    spec = request.spec.model_copy(
        update={
            "selection": request.spec.selection.model_copy(
                update={"accepted_before": accepted_before}
            )
        }
    )
    return AcceptedImageWork(
        operation_id=request.operation_id,
        source=None,
        work=ImageWorkReference(
            job_id=job_id,
            generation=1,
            owner=None,
            source_generation=0,
            kind="image_backfill",
            status="pending",
        ),
        spec=spec,
    )


async def _dataset(db_session, creator_id):
    dataset = Dataset(
        display_id=f"DS-AUTH-{uuid.uuid4().hex[:8]}",
        name=f"scene-auth-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        created_by=creator_id,
    )
    db_session.add(dataset)
    await db_session.flush()
    return dataset


async def _create_scene(db, dataset_id, name):
    result = await AssetsSpatialGateway(db).create_scene(
        CreateSceneRequest(
            operation_id=uuid.uuid4(),
            dataset_id=dataset_id,
            name=name,
            source_format=None,
            source_metadata={},
            created_by=None,
        ),
        same_session_native=True,
    )
    return result.scene


async def test_file_upload_init_denies_project_member_before_task_or_storage(
    db_session, httpx_client, super_admin, monkeypatch
):
    owner, owner_token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    employee = await create_user(
        db_session, "employee", f"file-init-{uuid.uuid4()}@test.local", "Uploader"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="annotator",
        assigned_by=owner.id,
    )
    signed = []
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_upload_url",
        lambda *args, **kwargs: (
            signed.append((args, kwargs)) or "https://storage.test/put"
        ),
    )
    body = {
        "project_id": str(project.id),
        "file_name": "frame.jpg",
        "content_type": "image/jpeg",
    }

    denied = await httpx_client.post(
        "/api/v1/files/upload-init", json=body, headers=_headers(employee)
    )

    assert denied.status_code == 403
    assert signed == []
    count = await db_session.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == project.id)
    )
    assert count == 0

    allowed = await httpx_client.post(
        "/api/v1/files/upload-init",
        json=body,
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert len(signed) == 1
    assert allowed.json()["upload_url"] == "https://storage.test/put"


async def test_file_upload_complete_denies_member_before_verify_or_write(
    db_session, httpx_client, super_admin, monkeypatch, media_dispatcher
):
    owner, owner_token = super_admin
    monkeypatch.setattr(
        "app.application.legacy_file_upload.ImageAdmission", _ImageAdmission
    )
    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("app.application.legacy_file_upload.image_policy", lambda: None)
    project = await create_project(db_session, owner_id=owner.id)
    employee = await create_user(
        db_session, "employee", f"file-complete-{uuid.uuid4()}@test.local", "Reviewer"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="uploading")
    verified = []
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.verify_upload",
        lambda *args, **kwargs: (
            verified.append((args, kwargs))
            or {"ContentLength": 10, "ETag": '"task-source"'}
        ),
    )
    denied = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}", headers=_headers(employee)
    )

    assert denied.status_code == 403
    assert verified == []
    assert media_dispatcher.task_thumbnails == []
    await db_session.refresh(task)
    assert task.status == "uploading"

    allowed = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"status": "ok", "task_id": str(task.id)}
    assert len(verified) == 1
    await db_session.refresh(task)
    assert task.status == "pending"
    assert media_dispatcher.task_thumbnails == []


async def test_file_url_uses_assigned_task_visibility(
    db_session, httpx_client, super_admin, monkeypatch
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    assigned = await create_user(
        db_session, "employee", f"file-visible-{uuid.uuid4()}@test.local", "Assigned"
    )
    outsider = await create_user(
        db_session, "employee", f"file-hidden-{uuid.uuid4()}@test.local", "Outsider"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=assigned.id,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="active")
    task.assignee_id = assigned.id
    await db_session.flush()
    signed = []
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_download_url",
        lambda *args, **kwargs: (
            signed.append((args, kwargs)) or "https://storage.test/get"
        ),
    )

    denied = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/file-url", headers=_headers(outsider)
    )

    assert denied.status_code == 404
    assert signed == []

    allowed = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/file-url", headers=_headers(assigned)
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"url": "https://storage.test/get", "expires_in": 3600}
    assert len(signed) == 1


async def test_thumbnail_backfill_requires_project_owner_before_enqueue(
    db_session, httpx_client, super_admin, monkeypatch
):
    owner, owner_token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    outsider = await create_user(
        db_session, "employee", f"backfill-{uuid.uuid4()}@test.local", "Outsider"
    )
    requests = []
    job_id = uuid.uuid4()

    class ImageOwner:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call(self, operation, request):
            assert operation == "schedule_image_media"
            requests.append(request)
            return _accepted_task_backfill(request, job_id=job_id)

    image_owner = ImageOwner()
    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway", lambda: image_owner
    )
    denied = await httpx_client.post(
        f"/api/v1/files/projects/{project.id}/backfill-thumbnails",
        headers=_headers(outsider),
    )

    assert denied.status_code == 404
    assert requests == []

    operation_id = uuid.uuid4()
    allowed = await httpx_client.post(
        f"/api/v1/files/projects/{project.id}/backfill-thumbnails",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": str(operation_id),
        },
    )
    assert allowed.status_code == 202, allowed.text
    assert allowed.json()["project_id"] == str(project.id)
    assert allowed.json()["operation_id"] == str(operation_id)
    assert allowed.json()["job_id"] == str(job_id)
    assert allowed.json()["purpose"] == "thumbnail"
    assert allowed.json()["status"] == "pending"
    assert allowed.json()["accepted_before"]
    assert allowed.headers["Idempotency-Key"] == str(operation_id)
    assert len(requests) == 1
    assert requests[0].operation_id == operation_id
    assert requests[0].spec.selection.owner_kind == "task"
    assert requests[0].spec.selection.project_id == project.id
    assert requests[0].spec.selection.purpose == "thumbnail"
    assert requests[0].spec.selection.only_missing is True


async def test_files_project_admin_requires_ownership(
    db_session, httpx_client, monkeypatch
):
    owner = await create_user(
        db_session, "project_admin", f"file-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    outsider = await create_user(
        db_session, "project_admin", f"file-other-{uuid.uuid4()}@test.local", "Other"
    )
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id, status="uploading")
    effects = []

    def reject_side_effect(*args, **kwargs):
        effects.append((args, kwargs))
        raise AssertionError("unauthorized storage or queue side effect")

    monkeypatch.setattr(
        "app.api.v1.files.storage_service.verify_upload", reject_side_effect
    )
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_download_url", reject_side_effect
    )
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_upload_url", reject_side_effect
    )

    def reject_image_admission():
        raise AssertionError("unauthorized image admission")

    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway", reject_image_admission
    )
    body = {"project_id": str(project.id), "file_name": "frame.jpg"}
    responses = [
        await httpx_client.post(
            "/api/v1/files/upload-init", json=body, headers=_headers(outsider)
        ),
        await httpx_client.post(
            f"/api/v1/files/upload-complete/{task.id}", headers=_headers(outsider)
        ),
        await httpx_client.get(
            f"/api/v1/files/tasks/{task.id}/file-url", headers=_headers(outsider)
        ),
        await httpx_client.post(
            f"/api/v1/files/projects/{project.id}/backfill-thumbnails",
            headers=_headers(outsider),
        ),
    ]
    assert all(response.status_code == 404 for response in responses)
    assert effects == []
    await db_session.refresh(task)
    assert task.status == "uploading"

    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_upload_url",
        lambda *_args: "https://storage.test/put",
    )
    allowed = await httpx_client.post(
        "/api/v1/files/upload-init", json=body, headers=_headers(owner)
    )
    assert allowed.status_code == 200, allowed.text


async def test_scene_reads_enforce_dataset_scope_for_api_keys(
    db_session, httpx_client, super_admin
):
    owner, _ = super_admin
    dataset = await _dataset(db_session, owner.id)
    scene = await _create_scene(db_session, dataset.id, "scope")
    _, no_scope_token = await create_api_key(db_session, owner.id, "scene-none", [])
    _, read_token = await create_api_key(
        db_session, owner.id, "scene-read", ["datasets:read"]
    )
    no_scope = {"Authorization": f"Bearer {no_scope_token}"}
    can_read = {"Authorization": f"Bearer {read_token}"}
    requests = (
        ("GET", f"/api/v1/scenes?dataset_id={dataset.id}"),
        ("GET", f"/api/v1/scenes/{scene.id}"),
        ("GET", f"/api/v1/scenes/{scene.id}/trajectory"),
    )

    for method, path in requests:
        denied = await httpx_client.request(method, path, headers=no_scope)
        assert denied.status_code == 403, (method, path, denied.text)
        allowed = await httpx_client.request(method, path, headers=can_read)
        assert allowed.status_code == 200, (method, path, allowed.text)


async def test_scene_patch_requires_dataset_manager_and_preserves_name_conflict(
    db_session, httpx_client, project_admin
):
    manager, manager_token = project_admin
    dataset = await _dataset(db_session, manager.id)
    scene = await _create_scene(db_session, dataset.id, "first")
    other = await _create_scene(db_session, dataset.id, "second")
    viewer = await create_user(
        db_session, "viewer", f"scene-viewer-{uuid.uuid4()}@test.local", "Viewer"
    )

    denied = await httpx_client.patch(
        f"/api/v1/scenes/{scene.id}",
        json={"name": "changed"},
        headers=_headers(viewer),
    )
    assert denied.status_code == 403
    unchanged = await AssetsSpatialGateway(db_session).get_scene(
        GetSceneRequest(actor_id=None, scene_id=scene.id)
    )
    assert unchanged.scene.name == "first"

    allowed = await httpx_client.patch(
        f"/api/v1/scenes/{scene.id}",
        json={"name": "changed"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["name"] == "changed"

    duplicate = await httpx_client.patch(
        f"/api/v1/scenes/{scene.id}",
        json={"name": other.name},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert duplicate.status_code == 409
    updated = await AssetsSpatialGateway(db_session).get_scene(
        GetSceneRequest(actor_id=None, scene_id=scene.id)
    )
    assert updated.scene.name == "changed"
