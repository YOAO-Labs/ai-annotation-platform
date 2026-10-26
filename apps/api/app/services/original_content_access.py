"""Fresh Task read authority and bounded locks around original-byte capture."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.enums import PlatformRole
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import comment_attachment_authority as credentials
from app.services import storage_access
from app.services.content_snapshot import ContentError


def credential_error(exc):
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
    if isinstance(code, str) and code.startswith("comment_attachment_"):
        return HTTPException(
            exc.status_code,
            {"code": code.replace("comment_attachment_", "content_", 1)},
        )
    return exc


async def capture_credentials(db, actor, request):
    if request is None:
        return None
    try:
        _, proof = await credentials.lock_authority(
            db, actor, request, allowed_roles=frozenset(PlatformRole)
        )
        return proof
    except HTTPException as exc:
        raise credential_error(exc) from None


async def actor_and_key(db, actor_id, key_id, scope, proof=None):
    try:
        actor = (
            await credentials.reauthorize(db, proof)
            if proof is not None
            else await credentials._actor(db, actor_id, frozenset(PlatformRole))
        )
        key = await credentials._key(db, actor_id, key_id)
    except HTTPException as exc:
        raise credential_error(exc) from None
    if key is not None and "*" not in key[1] and scope not in key[1]:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    return actor, key


def check_clock(proof, key):
    if proof is not None:
        try:
            credentials.check_clock(proof)
        except HTTPException as exc:
            raise credential_error(exc) from None
    if key is not None and key[2] is not None and key[2] <= datetime.now(timezone.utc):
        raise HTTPException(403, {"code": "api_key_authority_revoked"})


async def _row(db, model, identifier, *, lock):
    if identifier is None:
        return None
    if lock and model in {User, Project, Task}:
        # Visibility rows are rewritten by their own ordinary writers while a
        # read is in flight: task status/assignee/counters and project progress
        # or ownership. Waiting out those short writers under the existing
        # AUTHORITY_LOCK_TIMEOUT bound keeps the SHARE fence but lets the read
        # decide on the value they committed, instead of aborting as busy.
        try:
            return await credentials.locked(db, model, identifier, wait=True)
        except HTTPException as exc:
            raise credential_error(exc) from None
    statement = select(model).where(model.id == identifier)
    if lock:
        statement = statement.with_for_update(read=True, nowait=True)
    return await db.scalar(statement.execution_options(populate_existing=True))


async def visible_task(db, task_id, actor, *, lock=False):
    """Refresh Project, membership and Batch before the existing visibility rule.

    The actor, project and task rows take the existing SHARE lock through a
    bounded wait, so their own short counter/status writers (annotation
    bookkeeping, project progress, ownership transfer) finish and the decision
    uses the value they committed. That bound is a writer-collision budget, not
    an identity lock: membership and Batch keep the short SHARE NOWAIT fence,
    and every lock is released before entering the physical reader.
    """
    from app.api.v1.tasks._shared import _assert_task_visible
    from app.services.project_access import resolve_project_access

    try:
        with db.no_autoflush:
            located = (
                await db.execute(
                    select(Task.project_id, Task.batch_id).where(Task.id == task_id)
                )
            ).one_or_none()
            if located is None:
                raise HTTPException(404, "Task not found")
            project_id, batch_id = located
            # Without an HTTP proof this is still a fresh current actor; do not
            # grant visibility from an identity-map role retained across I/O.
            fresh_actor = await _row(db, User, actor.id, lock=lock)
            project = await _row(db, Project, project_id, lock=lock)
            if fresh_actor is None or not fresh_actor.is_active:
                raise HTTPException(401, {"code": "content_authority_revoked"})
            if project is None:
                raise HTTPException(404, "Task not found")
            membership = select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor.id,
            )
            if lock:
                membership = membership.with_for_update(read=True, nowait=True)
            await db.scalar(membership.execution_options(populate_existing=True))
            batch = await _row(db, TaskBatch, batch_id, lock=lock)
            task = await _row(db, Task, task_id, lock=lock)
            if task is None:
                raise HTTPException(404, "Task not found")
            if (
                task.project_id != project_id
                or task.batch_id != batch_id
                or (
                    batch_id is not None
                    and (batch is None or batch.project_id != project_id)
                )
            ):
                raise ContentError("content_binding_changed")
            access = await resolve_project_access(db, user=fresh_actor, project=project)
            await _assert_task_visible(db, task, fresh_actor, access=access)
            return task
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            raise ContentError("content_busy") from None
        raise


async def managed_dataset(db, item_id, actor, *, lock=False):
    from app.services.dataset_source_access import assert_dataset_manage

    try:
        with db.no_autoflush:
            dataset_id = await db.scalar(
                select(DatasetItem.dataset_id).where(DatasetItem.id == item_id)
            )
            if dataset_id is None:
                raise ContentError("item_removed")
            dataset = await _row(db, Dataset, dataset_id, lock=lock)
            if dataset is None:
                raise ContentError("item_removed")
            await assert_dataset_manage(db, dataset_id, actor)
            return dataset_id
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            raise ContentError("storage_original_busy") from None
        raise
