"""Managed Task uploads retain inferred file types and reject late source drift."""

from __future__ import annotations

import pytest

from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creations
from tests.test_task_upload_routes import (
    complete_url,
    headers,
    initialize,
    task_storage as task_storage,
    upload_project as upload_project,
)


@pytest.mark.parametrize(
    ("content_type", "file_type"),
    [
        ("image/png", "image"),
        ("video/mp4", "video"),
        ("application/octet-stream", "other"),
    ],
)
async def test_initialize_persists_inferred_file_type_on_managed_allocation(
    httpx_client,
    db_session,
    project_admin,
    upload_project,
    task_storage,
    content_type,
    file_type,
):
    task, allocation, params = await initialize(
        httpx_client,
        db_session,
        project_admin,
        upload_project,
        task_storage,
        content_type=content_type,
    )

    assert task.file_type == file_type
    assert task.status == "uploading"
    assert task.original_id is None
    assert task.file_path == allocation["object_key"]
    assert allocation["owner_kind"] == "task"
    assert allocation["dataset_id"] is None
    assert params["Key"] == allocation["staging_key"]
    assert task_storage.objects.get((params["Bucket"], params["Key"])) is None


async def test_complete_rejects_source_change_after_verified_creation(
    httpx_client,
    db_session,
    project_admin,
    upload_project,
    task_storage,
    monkeypatch,
):
    task, allocation, params = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"sealed candidate"
    original = creations.complete_direct

    async def changed(*args):
        result = await original(*args)
        task.file_path = allocation["object_key"] + "-replaced"
        await db_session.flush()
        return result

    monkeypatch.setattr(creations, "complete_direct", changed)
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "task_original_context_changed"
    ledger = await creations._read(db_session, allocation["id"])
    assert ledger["status"] == "verified"
    assert await db_session.get(TaskOriginal, allocation["id"]) is None
    refreshed = await db_session.get(Task, task.id, populate_existing=True)
    assert refreshed.status == "uploading" and refreshed.original_id is None
    assert not [call for call in task_storage.calls if call[0] == "delete"]
