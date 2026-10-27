"""Fresh Task read authority and bounded locks around original-byte capture."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.enums import PlatformRole
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
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
        # Identity confirms the accepted key, its current scopes and its expiry
        # under its own SHARE fence; no credential material is read here.
        key = await credentials._key(
            db,
            actor_id,
            key_id,
            required_scopes=(scope,) if key_id is not None else (),
        )
    except HTTPException as exc:
        raise credential_error(exc) from None
    if key is not None and "*" not in key[1] and scope not in key[1]:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    return actor, key


def check_clock(proof, key=None):
    if proof is not None:
        try:
            credentials.check_clock(proof)
        except HTTPException as exc:
            raise credential_error(exc) from None


async def _row(db, model, identifier, *, lock):
    if identifier is None:
        return None
    if lock and model in {User, Project}:
        # Non-core consumers reuse this helper for their legacy rows. Waiting
        # out the short self-service writers keeps the existing SHARE fence.
        try:
            return await credentials.locked(db, model, identifier, wait=True)
        except HTTPException as exc:
            raise credential_error(exc) from None
    statement = select(model).where(model.id == identifier)
    if lock:
        statement = statement.with_for_update(read=True, nowait=True)
    return await db.scalar(statement.execution_options(populate_existing=True))


async def visible_task(db, task_id, actor, *, lock=False):
    """Refresh the actor, Task, Project, membership and Batch before visibility.

    Owner reads provide every current fact. The caller's Task SHARE lock keeps
    the pre-existing order (Task before Project, Batch last) so an annotation
    writer that updates Task before Project counters is not drawn into a
    Project-to-Task inversion. All locks are released before physical I/O.
    """
    from app.services import task_access
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.labeling_core_read_relations import (
        batch_row,
        get_core_row,
        lock_batch_rows_for_core_read,
        lock_task_for_core_read,
        task_row,
    )
    from app.services.project_access import (
        get_workspace_project,
        lock_current_project_rows,
        resolve_project_access,
    )

    try:
        with db.no_autoflush:
            located = (
                await db.execute(
                    select(task_row.project_id, task_row.batch_id).where(
                        task_row.id == task_id
                    )
                )
            ).one_or_none()
            if located is None:
                raise HTTPException(404, "Task not found")
            project_id, batch_id = located
            # Without an HTTP proof this is still a fresh current actor; do not
            # grant visibility from an identity-map role retained across I/O.
            if lock:
                accounts = await IdentityAccountLockData(db).lock_accounts(
                    [actor.id], mode="share", nowait=True
                )
                fresh_actor = accounts.get(actor.id)
            else:
                fresh_actor = await IdentityAccountLockData(db).read_actor_status(
                    actor.id
                )
            # Annotation writers update Task before Project counters. Keep that
            # order so a read does not hold Project while waiting on their Task.
            if lock:
                task = await lock_task_for_core_read(
                    db,
                    task_id,
                    project_id=project_id,
                    lock_mode="share",
                    nowait=True,
                    populate_existing=True,
                )
                _, project = await lock_current_project_rows(
                    db, actor_id=actor.id, project_id=project_id
                )
            else:
                task = await get_core_row(db, task_row, task_id, populate_existing=True)
                project = await get_workspace_project(
                    db,
                    actor_id=actor.id,
                    project_id=project_id,
                    populate_existing=True,
                )
            if fresh_actor is None or not fresh_actor.is_active:
                raise HTTPException(401, {"code": "content_authority_revoked"})
            if project is None:
                raise HTTPException(404, "Task not found")
            access = await resolve_project_access(
                db,
                actor_id=actor.id,
                project=project,
                lock_membership=lock,
            )
            if lock:
                batch = (
                    (
                        await lock_batch_rows_for_core_read(
                            db,
                            [batch_id],
                            project_id=project_id,
                            lock_mode="share",
                            nowait=True,
                        )
                    )[0]
                    if batch_id is not None
                    else None
                )
            else:
                batch = (
                    await get_core_row(db, batch_row, batch_id, populate_existing=True)
                    if batch_id is not None
                    else None
                )
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
            await task_access.assert_task_visible(db, task, actor.id, access=access)
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
