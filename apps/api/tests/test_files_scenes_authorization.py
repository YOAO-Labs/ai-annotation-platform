"""Authorization boundaries for project files and dataset scenes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.db.models.dataset import Dataset
from app.db.models.task import Task
from app.services import scene as scene_svc
from app.services.api_key_service import create_key
from tests.factory import create_membership, create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


def _headers(user) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


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
    db_session, httpx_client, super_admin, monkeypatch
):
    owner, owner_token = super_admin
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
    thumbnails = []
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.verify_upload",
        lambda *args, **kwargs: (
            verified.append((args, kwargs)) or {"ContentLength": 10}
        ),
    )
    from app.workers import media

    monkeypatch.setattr(media.generate_task_thumbnail, "delay", thumbnails.append)

    denied = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}", headers=_headers(employee)
    )

    assert denied.status_code == 403
    assert verified == []
    assert thumbnails == []
    await db_session.refresh(task)
    assert task.status == "uploading"

    allowed = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"status": "ok", "task_id": str(task.id)}
    assert len(verified) == 1
    assert thumbnails == [str(task.id)]


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
    queued = []
    from app.workers import media

    monkeypatch.setattr(
        media.backfill_tasks,
        "delay",
        lambda *args: queued.append(args) or SimpleNamespace(id="backfill-test"),
    )

    denied = await httpx_client.post(
        f"/api/v1/files/projects/{project.id}/backfill-thumbnails",
        headers=_headers(outsider),
    )

    assert denied.status_code == 404
    assert queued == []

    allowed = await httpx_client.post(
        f"/api/v1/files/projects/{project.id}/backfill-thumbnails",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["celery_task_id"] == "backfill-test"
    assert queued == [(str(project.id),)]


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

    from app.workers import media

    monkeypatch.setattr(
        "app.api.v1.files.storage_service.verify_upload", reject_side_effect
    )
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_download_url", reject_side_effect
    )
    monkeypatch.setattr(
        "app.api.v1.files.storage_service.generate_upload_url", reject_side_effect
    )
    monkeypatch.setattr(media.backfill_tasks, "delay", reject_side_effect)
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
    scene = await scene_svc.create_scene(
        db_session, dataset_id=dataset.id, name="scope"
    )
    _, no_scope_token = await create_key(db_session, owner, "scene-none", [])
    _, read_token = await create_key(db_session, owner, "scene-read", ["datasets:read"])
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
    scene = await scene_svc.create_scene(
        db_session, dataset_id=dataset.id, name="first"
    )
    other = await scene_svc.create_scene(
        db_session, dataset_id=dataset.id, name="second"
    )
    viewer = await create_user(
        db_session, "viewer", f"scene-viewer-{uuid.uuid4()}@test.local", "Viewer"
    )

    denied = await httpx_client.patch(
        f"/api/v1/scenes/{scene.id}",
        json={"name": "changed"},
        headers=_headers(viewer),
    )
    assert denied.status_code == 403
    await db_session.refresh(scene)
    assert scene.name == "first"

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
    await db_session.refresh(scene)
    assert scene.name == "changed"
