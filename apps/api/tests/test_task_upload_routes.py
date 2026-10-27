"""Independent Task uploads publish only verified, durably owned originals."""

import hashlib
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import select

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creations
from app.services.storage import storage_service
from tests.fakes.managed_creation_storage import CreationStorage


def headers(actor):
    return {"Authorization": f"Bearer {actor[1]}"}


@pytest.fixture
def task_storage(monkeypatch):
    fake = CreationStorage(keyed_by_bucket=True)
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(creations, "create_client", lambda **_: fake)
    monkeypatch.setattr(
        "app.workers.media.generate_task_thumbnail.delay", lambda *_: None
    )
    return fake


@pytest.fixture
async def upload_project(db_session, project_admin):
    project = Project(
        display_id="pr-" + uuid.uuid4().hex[:15],
        name="Task original upload",
        owner_id=project_admin[0].id,
        type_key="bbox",
        type_label="Bounding box",
    )
    db_session.add(project)
    await db_session.flush()
    return project


async def initialize(client, db, actor, project, fake, *, content_type="image/png"):
    response = await client.post(
        "/api/v1/files/upload-init",
        headers=headers(actor),
        json={
            "project_id": str(project.id),
            "file_name": "folder/original.bin",
            "content_type": content_type,
        },
    )
    assert response.status_code == 200, response.text
    task_id = uuid.UUID(response.json()["task_id"])
    task = await db.get(Task, task_id)
    allocation = await creations.for_task(
        db, task_id=task_id, object_key=task.file_path
    )
    operation, params = fake.presigned[-1]
    assert operation == "put_object"
    assert params["Bucket"] == storage_service.import_bucket
    assert params["Key"] == allocation["staging_key"]
    assert params["Key"] != task.file_path
    assert allocation["bucket"] == storage_service.bucket
    assert allocation["dataset_id"] is None
    assert allocation["owner_kind"] == "task"
    return task, allocation, params


def complete_url(task):
    return f"/api/v1/files/upload-complete/{task.id}"


@pytest.mark.parametrize(
    "content_type", ["image/png", "video/mp4", "application/octet-stream"]
)
async def test_complete_retains_full_receipt_replay_and_task_deletion(
    httpx_client, db_session, project_admin, upload_project, task_storage, content_type
):
    task, allocation, params = await initialize(
        httpx_client,
        db_session,
        project_admin,
        upload_project,
        task_storage,
        content_type=content_type,
    )
    data = b"accepted original"
    fake = task_storage
    fake.objects[(params["Bucket"], params["Key"])] = data
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert task.status == "pending" and task.original_id == allocation["id"]
    owner = await db_session.get(TaskOriginal, task.original_id)
    assert owner.creation_id == allocation["id"]
    assert owner.sha256 == hashlib.sha256(data).hexdigest()
    assert owner.size_bytes == len(data)
    assert (owner.bucket, owner.object_key) == (storage_service.bucket, task.file_path)
    ledger = await creations._read(db_session, allocation["id"])
    assert ledger["status"] == "verified" and ledger["receipt"]
    assert ledger["verification"] and ledger["settled_at"]
    completed = len([call for call in fake.calls if call[0] == "complete"])
    fake.objects[(params["Bucket"], params["Key"])] = b"replayed staging"
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    assert fake.objects[(owner.bucket, owner.object_key)] == data
    assert len([call for call in fake.calls if call[0] == "complete"]) == completed
    owner_id = owner.id
    await db_session.delete(task)
    await db_session.commit()
    assert await db_session.get(TaskOriginal, owner_id, populate_existing=True)
    assert await creations._read(db_session, allocation["id"])
    assert not [call for call in fake.calls if call[0] == "delete"]


@pytest.mark.parametrize("existing", [b"same bytes", b"other owner"])
async def test_final_collision_never_adopts_or_deletes(
    httpx_client, db_session, project_admin, upload_project, task_storage, existing
):
    task, allocation, params = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    fake = task_storage
    fake.objects[(params["Bucket"], params["Key"])] = b"same bytes"
    final = (allocation["bucket"], task.file_path)
    fake.objects[final] = existing
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 409, response.text
    assert fake.objects[final] == existing
    assert task.original_id is None and task.status == "uploading"
    assert (await creations._read(db_session, allocation["id"]))["status"] == "conflict"
    assert await db_session.get(TaskOriginal, allocation["id"]) is None
    assert not [call for call in fake.calls if call[0] == "delete"]


async def test_visible_employee_cannot_initialize(
    httpx_client, annotator, upload_project, task_storage
):
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=headers(annotator),
        json={"project_id": str(upload_project.id), "file_name": "a.png"},
    )
    assert response.status_code == 403
    assert task_storage.presigned == [] and task_storage.calls == []


@pytest.mark.parametrize("name", ["", "../escape.png", "/absolute.png", "a" * 501])
async def test_invalid_name_has_no_allocation_or_io(
    httpx_client, project_admin, upload_project, task_storage, name
):
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=headers(project_admin),
        json={"project_id": str(upload_project.id), "file_name": name},
    )
    assert response.status_code == 422
    assert task_storage.presigned == [] and task_storage.calls == []


async def test_other_manager_cannot_complete_original_uploader(
    httpx_client, db_session, project_admin, super_admin, upload_project, task_storage
):
    task, allocation, _ = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    response = await httpx_client.post(complete_url(task), headers=headers(super_admin))
    assert response.status_code == 403
    assert task_storage.calls == []
    assert (await creations._read(db_session, allocation["id"]))[
        "status"
    ] == "allocated"


@pytest.mark.parametrize(
    "change", ["task_deleted", "account_disabled", "project_transferred"]
)
async def test_late_authority_or_context_change_preserves_verified_creation(
    httpx_client,
    db_session,
    project_admin,
    super_admin,
    upload_project,
    task_storage,
    monkeypatch,
    change,
):
    task, allocation, params = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"sealed bytes"
    original = creations.complete_direct

    async def changed(*args):
        result = await original(*args)
        if change == "task_deleted":
            await db_session.delete(task)
        elif change == "account_disabled":
            project_admin[0].is_active = False
        else:
            upload_project.owner_id = super_admin[0].id
        await db_session.flush()
        return result

    monkeypatch.setattr(creations, "complete_direct", changed)
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code in {403, 404}, response.text
    ledger = await creations._read(db_session, allocation["id"])
    assert ledger["status"] == "verified"
    assert (
        task_storage.objects[(ledger["bucket"], ledger["object_key"])]
        == b"sealed bytes"
    )
    assert await db_session.get(TaskOriginal, allocation["id"]) is None
    assert not [call for call in task_storage.calls if call[0] == "delete"]


async def test_unknown_legacy_upload_requires_explicit_proof_without_storage_io(
    httpx_client, db_session, project_admin, upload_project, task_storage
):
    task = Task(
        display_id="ta-" + uuid.uuid4().hex[:15],
        project_id=upload_project.id,
        file_name="old.png",
        file_path="historical-key",
        file_type="image",
        status="uploading",
    )
    db_session.add(task)
    await db_session.flush()
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "task_original_creation_required"
    assert task_storage.calls == []


def test_task_final_namespace_never_gets_client_put_url(task_storage):
    with pytest.raises(ValueError, match="controlled server creation"):
        storage_service.generate_upload_url(
            f"__aap_task_originals__/{uuid.uuid4()}/{uuid.uuid4()}/payload"
        )
    assert task_storage.presigned == []


async def test_failed_final_publication_keeps_independent_owner_after_rollback(
    httpx_client, db_session, project_admin, upload_project, task_storage, monkeypatch
):
    from app.services import comment_attachment_authority as credentials
    from fastapi import HTTPException

    task, allocation, params = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"retained candidate"
    original = credentials.reauthorize
    count = 0

    async def revoked(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
        return await original(*args, **kwargs)

    monkeypatch.setattr(credentials, "reauthorize", revoked)
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 403, response.text
    await db_session.rollback()
    async with creations.storage_access.engine(db_session).connect() as connection:
        owner = (
            (
                await connection.execute(
                    select(TaskOriginal.__table__).where(
                        TaskOriginal.id == allocation["id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    assert owner["sha256"] == hashlib.sha256(b"retained candidate").hexdigest()
    assert (await creations._read(db_session, allocation["id"]))["status"] == "verified"


async def test_initialize_expiry_during_commit_does_not_return_put_url(
    httpx_client, db_session, project_admin, upload_project, task_storage, monkeypatch
):
    from app.services import comment_attachment_authority as credentials

    original = db_session.commit

    async def expired_commit():
        await original()
        monkeypatch.setattr(
            credentials, "now", lambda: datetime(2100, 1, 1, tzinfo=timezone.utc)
        )

    monkeypatch.setattr(db_session, "commit", expired_commit)
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=headers(project_admin),
        json={"project_id": str(upload_project.id), "file_name": "a.bin"},
    )
    assert response.status_code == 403, response.text
    assert "upload_url" not in response.json()
    assert task_storage.calls == []


@pytest.mark.parametrize("commit_number", [1, 2])
async def test_complete_expiry_during_commit_never_returns_success(
    httpx_client,
    db_session,
    project_admin,
    upload_project,
    task_storage,
    monkeypatch,
    commit_number,
):
    from app.services import comment_attachment_authority as credentials

    task, allocation, params = await initialize(
        httpx_client, db_session, project_admin, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"valid candidate"
    original = db_session.commit
    count = 0

    async def expired_commit():
        nonlocal count
        await original()
        count += 1
        if count == commit_number:
            monkeypatch.setattr(
                credentials, "now", lambda: datetime(2100, 1, 1, tzinfo=timezone.utc)
            )

    monkeypatch.setattr(db_session, "commit", expired_commit)
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 403, response.text
    ledger = await creations._read(db_session, allocation["id"])
    if commit_number == 1:
        assert ledger["status"] == "allocated"
        assert task_storage.calls == []
    else:
        assert ledger["status"] == "verified"
        assert await db_session.get(TaskOriginal, allocation["id"])


async def make_key(client, actor, scopes):
    response = await client.post(
        "/api/v1/me/api-keys",
        headers=headers(actor),
        json={"name": "Task upload key", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_api_key_scope_and_original_key_identity_are_required(
    httpx_client, db_session, project_admin, upload_project, task_storage
):
    read_key = await make_key(httpx_client, project_admin, ["tasks:read"])
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers={"Authorization": "Bearer " + read_key["plaintext"]},
        json={"project_id": str(upload_project.id), "file_name": "a.bin"},
    )
    assert response.status_code == 403 and task_storage.presigned == []
    key = await make_key(httpx_client, project_admin, ["tasks:write"])
    actor = (project_admin[0], key["plaintext"])
    task, allocation, params = await initialize(
        httpx_client, db_session, actor, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"key bound original"
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 403 and task_storage.calls == []
    second = await make_key(httpx_client, project_admin, ["tasks:write"])
    response = await httpx_client.post(
        complete_url(task), headers={"Authorization": "Bearer " + second["plaintext"]}
    )
    assert response.status_code == 403 and task_storage.calls == []
    response = await httpx_client.post(complete_url(task), headers=headers(actor))
    assert response.status_code == 200, response.text
    assert await db_session.get(TaskOriginal, allocation["id"])


@pytest.mark.parametrize(
    "change", ["revoked", "rotated", "scope_removed", "expires_changed"]
)
async def test_original_key_changes_after_io_preserve_candidate_without_publication(
    httpx_client,
    db_session,
    project_admin,
    upload_project,
    task_storage,
    monkeypatch,
    change,
):
    from app.db.models.api_key import ApiKey

    key = await make_key(httpx_client, project_admin, ["tasks:write"])
    actor = (project_admin[0], key["plaintext"])
    task, allocation, params = await initialize(
        httpx_client, db_session, actor, upload_project, task_storage
    )
    task_storage.objects[(params["Bucket"], params["Key"])] = b"unpublished candidate"
    original = creations.complete_direct

    async def changed(*args):
        result = await original(*args)
        row = await db_session.get(ApiKey, uuid.UUID(key["id"]))
        if change == "revoked":
            row.revoked_at = datetime.now(timezone.utc)
        elif change == "rotated":
            row.key_hash = "different proof"
        elif change == "scope_removed":
            row.scopes = []
        else:
            row.expires_at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        await db_session.flush()
        return result

    monkeypatch.setattr(creations, "complete_direct", changed)
    response = await httpx_client.post(complete_url(task), headers=headers(actor))
    assert response.status_code == 403, response.text
    assert (await creations._read(db_session, allocation["id"]))["status"] == "verified"
    assert await db_session.get(TaskOriginal, allocation["id"]) is None
    assert task.original_id is None and task.status == "uploading"
    assert not [call for call in task_storage.calls if call[0] == "delete"]


async def test_project_delete_keeps_original_owner_creation_and_bytes(
    httpx_client, db_session, project_admin, upload_project, task_storage
):
    task, allocation, params = await initialize(
        httpx_client,
        db_session,
        project_admin,
        upload_project,
        task_storage,
        content_type="application/octet-stream",
    )
    task_id, project_id = task.id, upload_project.id
    task_storage.objects[(params["Bucket"], params["Key"])] = b"project original"
    response = await httpx_client.post(
        complete_url(task), headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.delete(
        f"/api/v1/projects/{project_id}", headers=headers(project_admin)
    )
    assert response.status_code == 204, response.text
    db_session.expire_all()
    assert await db_session.get(Project, project_id) is None
    assert await db_session.get(Task, task_id) is None
    owner = await db_session.get(TaskOriginal, allocation["id"])
    assert owner and owner.task_id == task_id and owner.project_id == project_id
    assert (await creations._read(db_session, allocation["id"]))["status"] == "verified"
    assert task_storage.objects[(owner.bucket, owner.object_key)] == b"project original"
    assert not [call for call in task_storage.calls if call[0] == "delete"]
