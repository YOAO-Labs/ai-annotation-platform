from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from fastapi import HTTPException
from aap_assets_images.contract import ImageFailure
from sqlalchemy import event, func, inspect, select, update

from app.application.legacy_file_upload import (
    LegacyFileUploadWorkflow,
    UploadTaskNotUploadingError,
)
from app.application.task_workflow_errors import TaskWorkflowError
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.storage import storage_service
from app.services.labeling_tasks import (
    LabelingTaskService,
    LabelingUploadTarget,
    LabelingUploadTaskNotFound,
    LabelingUploadTaskNotUploading,
    LabelingUploadTargetChanged,
)
from tests.factory import build_tool_bindings, create_membership

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "reply,error",
    [
        ("pending", None),
        ("task_not_found", LabelingUploadTaskNotFound),
        ("task_not_uploading", LabelingUploadTaskNotUploading),
        ("task_target_changed", LabelingUploadTargetChanged),
        (None, TaskWorkflowError),
    ],
)
async def test_upload_owner_completion_keeps_caller_transaction_and_clean_cache(
    reply, error
):
    task = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="uploading",
        file_path="source.jpg",
        file_type="image",
    )
    target = LabelingUploadTarget(
        task.id, task.project_id, None, task.file_path, task.file_type, task.status
    )
    db = Mock(
        flush=AsyncMock(), scalar=AsyncMock(return_value=reply), commit=AsyncMock()
    )
    service = LabelingTaskService(db)
    if error is not None:
        with pytest.raises(error):
            await service.complete_uploading_task(task, target)
        assert task.status == "uploading"
    else:
        await service.complete_uploading_task(task, target)
        assert task.status == "pending"
        assert not inspect(task).attrs.status.history.has_changes()
        db.expire.assert_called_once_with(task, ["updated_at"])
    db.commit.assert_not_called()


async def test_upload_owner_bad_initialization_receipt_never_commits():
    db = Mock(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=uuid.uuid4()),
        commit=AsyncMock(),
    )
    with pytest.raises(TaskWorkflowError) as error:
        await LabelingTaskService(db).create_uploading_task(
            task_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            file_name="source.jpg",
            file_path="source.jpg",
            file_type="image",
        )
    assert error.value.status_code == 503
    db.commit.assert_not_called()


class _ImageAdmission:
    calls = []

    def __init__(self, *_args):
        pass

    async def admit_task(self, **request):
        self.calls.append(request)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_project(db_session, owner_id: uuid.UUID) -> Project:
    project = Project(
        display_id=f"P-FILE-{uuid.uuid4().hex[:8]}",
        name=f"legacy-file-{uuid.uuid4().hex[:8]}",
        type_key="image-segmentation",
        type_label="Image",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["defect"], unit="region"),
    )
    db_session.add(project)
    await db_session.flush()
    return project


@pytest.mark.parametrize(
    ("content_type", "file_type"),
    [
        ("image/jpeg", "image"),
        ("video/mp4", "video"),
        ("application/octet-stream", "other"),
    ],
)
async def test_upload_init_persists_inferred_file_type_in_project_scope(
    httpx_client, super_admin, db_session, monkeypatch, content_type, file_type
):
    user, token = super_admin
    project = await _make_project(db_session, user.id)
    monkeypatch.setattr(
        storage_service,
        "generate_upload_url",
        lambda *args: "https://uploads.test/signed",
    )
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=_bearer(token),
        json={
            "project_id": str(project.id),
            "file_name": "source.data",
            "content_type": content_type,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    task_id = uuid.UUID(body["task_id"])
    task = await db_session.get(Task, task_id)
    assert task is not None
    assert task.status == "uploading"
    assert task.file_type == file_type
    assert task.file_path == f"{project.id}/{task_id}/source.data"


async def test_verified_upload_becomes_pending(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    _ImageAdmission.calls = []
    monkeypatch.setattr(
        "app.application.legacy_file_upload.ImageAdmission", _ImageAdmission
    )
    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("app.application.legacy_file_upload.image_policy", lambda: None)
    project = await _make_project(db_session, user.id)
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key: {"ContentLength": 12, "ETag": '"task-source"'},
    )
    monkeypatch.setattr(
        storage_service,
        "generate_upload_url",
        lambda key, content_type: "https://uploads.test/image",
    )
    initialized = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=_bearer(token),
        json={
            "project_id": str(project.id),
            "file_name": "source.jpg",
            "content_type": "image/jpeg",
        },
    )
    assert initialized.status_code == 200, initialized.text
    task_id = uuid.UUID(initialized.json()["task_id"])

    completed = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task_id}", headers=_bearer(token)
    )

    assert completed.status_code == 200, completed.text
    task = await db_session.get(Task, task_id)
    assert task is not None and task.status == "pending"
    assert len(_ImageAdmission.calls) == 1
    assert _ImageAdmission.calls[0]["task_id"] == task_id
    assert _ImageAdmission.calls[0]["file_path"] == task.file_path
    assert _ImageAdmission.calls[0]["object_identity"].etag == "task-source"


async def test_image_admission_failure_rolls_back_task_pending_transition(
    db_session, project_admin, monkeypatch, media_dispatcher
):
    owner, _ = project_admin
    project = await _make_project(db_session, owner.id)
    task = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="source.jpg",
        file_path=f"{project.id}/source.jpg",
        file_type="image",
        status="uploading",
    )
    db_session.add(task)
    await db_session.flush()
    await db_session.commit()
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key: {"ContentLength": 12, "ETag": '"task-source"'},
    )

    class _RejectAdmission:
        def __init__(self, *_args):
            pass

        async def admit_task(self, **_request):
            assert task.status == "pending"
            assert db_session.in_transaction()
            raise ImageFailure("source_changed")

    monkeypatch.setattr(
        "app.application.legacy_file_upload.ImageAdmission", _RejectAdmission
    )
    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("app.application.legacy_file_upload.image_policy", lambda: None)

    with pytest.raises(TaskWorkflowError) as error:
        await LegacyFileUploadWorkflow(
            db_session, storage=storage_service
        ).complete_upload(
            actor_id=owner.id,
            task_id=task.id,
        )

    assert error.value.status_code == 409
    await db_session.rollback()
    await db_session.refresh(task)
    assert task.status == "uploading"
    assert media_dispatcher.task_thumbnails == []


async def test_upload_complete_preserves_status_and_missing_object_errors(
    httpx_client, super_admin, db_session, monkeypatch, media_dispatcher
):
    user, token = super_admin
    project = await _make_project(db_session, user.id)
    pending = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="pending.jpg",
        file_path="pending.jpg",
        file_type="image",
        status="pending",
    )
    uploading = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="uploading.jpg",
        file_path="uploading.jpg",
        file_type="image",
        status="uploading",
    )
    db_session.add_all([pending, uploading])
    await db_session.flush()
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key: None,
    )

    missing_task = await httpx_client.post(
        f"/api/v1/files/upload-complete/{uuid.uuid4()}", headers=_bearer(token)
    )
    wrong_state = await httpx_client.post(
        f"/api/v1/files/upload-complete/{pending.id}", headers=_bearer(token)
    )
    missing_object = await httpx_client.post(
        f"/api/v1/files/upload-complete/{uploading.id}", headers=_bearer(token)
    )

    assert missing_task.status_code == 404
    assert wrong_state.status_code == 400
    assert missing_object.status_code == 400
    assert uploading.status == "uploading"
    assert media_dispatcher.task_thumbnails == []


async def _file_operation(client, token, project_id, task_id, operation):
    headers = _bearer(token)
    if operation == "initialize":
        return await client.post(
            "/api/v1/files/upload-init",
            headers=headers,
            json={
                "project_id": str(project_id),
                "file_name": "new.png",
                "content_type": "image/png",
            },
        )
    if operation == "complete":
        return await client.post(
            f"/api/v1/files/upload-complete/{task_id}", headers=headers
        )
    if operation == "backfill":
        return await client.post(
            f"/api/v1/files/projects/{project_id}/backfill-thumbnails",
            headers=headers,
        )
    assert operation == "read"
    return await client.get(f"/api/v1/files/tasks/{task_id}/file-url", headers=headers)


@pytest.mark.parametrize("operation", ["initialize", "complete", "backfill", "read"])
async def test_outsider_cannot_issue_uploads_finalize_or_read_foreign_media(
    httpx_client,
    db_session,
    super_admin,
    annotator,
    monkeypatch,
    operation,
):
    owner, _ = super_admin
    _, token = annotator
    project = await _make_project(db_session, owner.id)
    task = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="source.png",
        file_path=f"{project.id}/source.png",
        file_type="image",
        status="uploading",
    )
    db_session.add(task)
    await db_session.flush()
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key: {"ContentLength": 12, "ETag": '"task-source"'},
    )
    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        lambda key: "https://objects.test/source",
    )

    def reject_image_admission():
        raise AssertionError("unauthorized image admission")

    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway", reject_image_admission
    )
    response = await _file_operation(
        httpx_client, token, project.id, task.id, operation
    )
    assert response.status_code == 404
    await db_session.refresh(task)
    assert task.status == "uploading"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 1
    )


@pytest.mark.parametrize("operation", ["initialize", "complete", "backfill"])
async def test_assigned_annotator_cannot_manage_legacy_uploads(
    httpx_client,
    db_session,
    super_admin,
    annotator,
    monkeypatch,
    operation,
):
    owner, _ = super_admin
    member, token = annotator
    project = await _make_project(db_session, owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    task = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="source.png",
        file_path=f"{project.id}/source.png",
        file_type="image",
        status="uploading",
        assignee_id=member.id,
        assignee_is_override=True,
    )
    db_session.add(task)
    await db_session.flush()
    monkeypatch.setattr(
        storage_service, "verify_upload", lambda key: {"ContentLength": 12}
    )

    def reject_image_admission():
        raise AssertionError("unauthorized image admission")

    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway", reject_image_admission
    )
    response = await _file_operation(
        httpx_client, token, project.id, task.id, operation
    )
    assert response.status_code == 403
    await db_session.refresh(task)
    assert task.status == "uploading"


async def test_file_url_preserves_non_manager_task_visibility(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    member, token = annotator
    project = await _make_project(db_session, owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    visible = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="visible.png",
        file_path="visible.png",
        file_type="image",
        status="pending",
        assignee_id=member.id,
        assignee_is_override=True,
    )
    hidden = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="hidden.png",
        file_path="hidden.png",
        file_type="image",
        status="pending",
    )
    db_session.add_all([visible, hidden])
    await db_session.flush()
    allowed = await _file_operation(httpx_client, token, project.id, visible.id, "read")
    denied = await _file_operation(httpx_client, token, project.id, hidden.id, "read")
    assert allowed.status_code == 200
    assert denied.status_code == 404


@pytest.mark.parametrize(
    "change", ["owner_transferred", "actor_disabled", "status_changed"]
)
async def test_upload_completion_rechecks_cached_authority_and_task_state(
    db_session, project_admin, super_admin, monkeypatch, media_dispatcher, change
):
    owner, _ = project_admin
    successor, _ = super_admin
    project = await _make_project(db_session, owner.id)
    task = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="source.png",
        file_path="source.png",
        file_type="image",
        status="uploading",
    )
    db_session.add(task)
    await db_session.flush()
    if change == "owner_transferred":
        mutation = (
            update(Project)
            .where(Project.id == project.id)
            .values(owner_id=successor.id)
        )
        expected_status = 404
    elif change == "actor_disabled":
        mutation = update(User).where(User.id == owner.id).values(is_active=False)
        expected_status = 401
    else:
        mutation = update(Task).where(Task.id == task.id).values(status="review")
        expected_status = None
    await db_session.execute(mutation.execution_options(synchronize_session=False))
    monkeypatch.setattr(
        storage_service, "verify_upload", lambda key: {"ContentLength": 12}
    )
    expected_error = (
        UploadTaskNotUploadingError if change == "status_changed" else HTTPException
    )
    with pytest.raises(expected_error) as error:
        await LegacyFileUploadWorkflow(
            db_session, storage=storage_service
        ).complete_upload(
            actor_id=owner.id,
            task_id=task.id,
        )
    if expected_status is not None:
        assert error.value.status_code == expected_status
    await db_session.refresh(task)
    assert task.status == ("review" if change == "status_changed" else "uploading")
    assert media_dispatcher.task_thumbnails == []


async def test_upload_completion_rejects_source_changed_after_storage_verification(
    httpx_client, db_session, test_engine, super_admin, monkeypatch, media_dispatcher
):
    owner, token = super_admin
    project = await _make_project(db_session, owner.id)
    task = Task(
        project_id=project.id,
        display_id=f"T-FILE-{uuid.uuid4().hex[:8]}",
        file_name="source.png",
        file_path=f"{project.id}/source.png",
        file_type="image",
        status="uploading",
    )
    db_session.add(task)
    await db_session.flush()
    boundary = {"verified": False, "changed": False}

    def verify_upload(key):
        boundary["verified"] = True
        return {"ContentLength": 12, "ETag": '"task-source"'}

    def change_source(connection, cursor, statement, parameters, context, executemany):
        if boundary["verified"] and not boundary["changed"]:
            boundary["changed"] = True
            connection.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(file_path=f"{project.id}/replacement.png")
            )

    monkeypatch.setattr(storage_service, "verify_upload", verify_upload)
    event.listen(test_engine.sync_engine, "before_cursor_execute", change_source)
    try:
        response = await _file_operation(
            httpx_client, token, project.id, task.id, "complete"
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", change_source)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "task_target_changed"
    await db_session.refresh(task)
    assert task.status == "uploading"
    assert media_dispatcher.task_thumbnails == []
