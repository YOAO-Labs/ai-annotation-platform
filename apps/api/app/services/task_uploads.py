"""Publish independent Task uploads without trusting a client final-key PUT."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.enums import PlatformRole
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import comment_attachment_authority as credentials
from app.services import managed_original_creation as creations
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import MAX_DIRECT_UPLOAD_BYTES
from app.services.dataset_storage_keys import normalize_dataset_relative_path
from app.services.display_id import next_display_id
from app.services.project_access import resolve_project_access
from app.services.storage import storage_service


MANAGERS = {PlatformRole.SUPER_ADMIN, PlatformRole.PROJECT_ADMIN}


def reject(code, status=409):
    raise HTTPException(status, {"code": code})


def http_error(exc):
    if isinstance(exc, HTTPException):
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        if isinstance(code, str) and code.startswith("comment_attachment_"):
            return HTTPException(
                exc.status_code,
                {"code": code.replace("comment_attachment_", "task_original_", 1)},
            )
        return exc
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
        return HTTPException(409, {"code": "task_original_busy"})
    return HTTPException(503, {"code": "task_original_unavailable"})


async def project_context(db, actor, project_id):
    project = await credentials.locked(db, Project, project_id)
    if project is None:
        reject("task_original_project_missing", 404)
    access = await resolve_project_access(db, user=actor, project=project)
    if not access.is_manager:
        reject("task_original_manager_required", 403)
    return project


async def task_context(db, actor, task_id):
    with db.no_autoflush:
        project_id = await db.scalar(select(Task.project_id).where(Task.id == task_id))
    if project_id is None:
        reject("task_original_task_missing", 404)
    await project_context(db, actor, project_id)
    task = await credentials.locked(db, Task, task_id, read=False)
    if task is None:
        reject("task_original_task_missing", 404)
    if task.project_id != project_id:
        reject("task_original_context_changed")
    linked = await db.scalar(
        select(TaskDatasetItemLink.task_id)
        .where(TaskDatasetItemLink.task_id == task_id)
        .limit(1)
    )
    if task.dataset_item_id is not None or linked is not None:
        reject("task_original_dataset_backed")
    facts = (
        task.id,
        task.project_id,
        task.file_name,
        task.file_path,
        task.file_type,
        task.status,
        task.original_id,
    )
    return task, facts


def actor_facts(actor, proof):
    return {
        "actor_id": str(actor.id),
        "api_key_id": str(proof.key_id) if proof.key_id else None,
    }


async def initialize(db, actor, request, data, file_type):
    if (
        data.file_name.startswith(("/", "\\"))
        or data.file_name.endswith(("/", "\\"))
        or any(
            ord(character) < 32 or ord(character) == 127 for character in data.file_name
        )
    ):
        reject("task_original_invalid_name", 422)
    try:
        logical_path = normalize_dataset_relative_path(data.file_name)
    except (AttributeError, TypeError, ValueError):
        reject("task_original_invalid_name", 422)
    actor, proof = await credentials.lock_authority(
        db, actor, request, allowed_roles=MANAGERS
    )
    await project_context(db, actor, data.project_id)
    task_id = uuid.uuid4()
    allocation = await creations.allocate_task(
        db,
        project_id=data.project_id,
        task_id=task_id,
        logical_path=logical_path,
        max_bytes=MAX_DIRECT_UPLOAD_BYTES,
        requested_by=actor.id,
        authority=actor_facts(actor, proof),
        content_type=data.content_type,
    )
    task = Task(
        id=task_id,
        project_id=data.project_id,
        display_id=await next_display_id(db, "tasks"),
        file_name=logical_path,
        file_path=allocation["object_key"],
        file_type=file_type,
        status="uploading",
    )
    db.add(task)
    actor = await credentials.reauthorize(db, proof)
    await project_context(db, actor, data.project_id)
    await creations._validate(db, allocation, lock=True)
    url = storage_service.generate_upload_url(
        allocation["staging_key"],
        data.content_type,
        bucket=allocation["staging_bucket"],
    )
    credentials.check_clock(proof)
    await db.commit()
    credentials.check_clock(proof)
    return task_id, url


async def complete(db, actor, request, task_id):
    actor, proof = await credentials.lock_authority(
        db, actor, request, allowed_roles=MANAGERS
    )
    task, before = await task_context(db, actor, task_id)
    allocation = await creations.for_task(
        db, task_id=task_id, object_key=task.file_path
    )
    if allocation is None:
        reject("task_original_creation_required")
    if (
        allocation["requested_by"] != actor.id
        or allocation["project_id"] != task.project_id
        or allocation["authority"] != actor_facts(actor, proof)
    ):
        reject("task_original_uploader_required", 403)
    if task.status != "uploading" and task.original_id != allocation["id"]:
        reject("task_original_context_changed")
    # No business/credential locks may span physical I/O. The independent
    # creation ledger retains responsibility if publication later fails.
    await db.commit()
    credentials.check_clock(proof)
    created = await creations.complete_direct(db, allocation)
    actor = await credentials.reauthorize(db, proof)
    task, after = await task_context(db, actor, task_id)
    if after != before:
        reject("task_original_context_changed")
    from app.services.task_originals import publish

    await publish(db, task, allocation, created)
    task.status = "pending" if task.status == "uploading" else task.status
    await credentials.reauthorize(db, proof)
    await db.commit()
    credentials.check_clock(proof)
    return task.file_type
