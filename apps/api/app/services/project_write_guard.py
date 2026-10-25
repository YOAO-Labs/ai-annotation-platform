"""Workspace authority locks and task-phase admission for final writes.

Long-running workers (cross-frame propagation, video tracker) must re-check the
*actual* task the write lands on, not the authority that was checked when work
was enqueued.  This module composes the existing canonical helpers:

* current project access (locked membership) from
  :mod:`app.services.project_access`;
* task editability and effective-assignee rules from
  :mod:`app.services.task_access`;
* frozen review contributor evidence from
  :mod:`app.services.annotation_evidence`.

Project-manager commands share the account/project lock and current-management
revalidation here; the caller retains the transaction and releases those locks.

It deliberately adds no new policy language: an annotation-phase write requires
the effective annotator (or a manager), and a review-phase write requires a
reviewer/manager plus current, complete, non-self frozen evidence.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from app.db.models.user import User
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.project_access import (
    bind_project_write_policy_resolver,
    get_workspace_project,
    restore_project_write_policy_failure,
)
from app.services.workspace_member_locks import lock_project_member_reference
from aap_workspace_contract.access import (
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
)

_BUSY_SQLSTATES = {"55P03", "40001", "40P01"}
_LOCK_PROJECT_REFERENCE = text(
    "SELECT aap_workspace_v1.lock_project_reference(CAST(:project_id AS uuid))"
)
_LOCK_PROJECT_REFERENCE_NOWAIT = text(
    "SELECT aap_workspace_v1.lock_project_reference_nowait(CAST(:project_id AS uuid))"
)


def _busy_lock_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate in _BUSY_SQLSTATES


async def lock_actor_account(
    db: AsyncSession, actor_id, *, nowait: bool = True
) -> None:
    """Account-first bounded FOR SHARE lock on the acting account."""

    await lock_actor_account_facts(db, actor_id, nowait=nowait)


async def lock_actor_account_facts(
    db: AsyncSession, actor_id, *, nowait: bool = True
) -> IdentityActorFacts | None:
    """Hold the account SHARE lock and return only its current Identity facts."""

    from sqlalchemy.exc import DBAPIError

    try:
        accounts = await IdentityAccountLockData(db).lock_accounts(
            [actor_id] if actor_id is not None else [], mode="share", nowait=nowait
        )
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise HTTPException(
                status_code=409, detail={"reason": "account_busy"}
            ) from exc
        raise
    return accounts.get(actor_id)


async def lock_actor_scope(
    db: AsyncSession, actor_id, project_id, *, nowait: bool = True
) -> None:
    """Bounded account -> project -> membership share locks for a final write."""

    from sqlalchemy.exc import DBAPIError

    await lock_actor_project_authority(db, actor_id, project_id, nowait=nowait)
    try:
        await lock_project_member_reference(db, project_id, actor_id, nowait=nowait)
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise HTTPException(
                status_code=409, detail={"reason": "membership_busy"}
            ) from exc
        raise


async def lock_actor_project_authority(
    db: AsyncSession, actor_id, project_id, *, nowait: bool = True
) -> IdentityActorFacts | None:
    """Account-first bounded share locks covering owner-based authority.

    Order is account -> project row, matching the lifecycle model.  The project
    row KEY SHARE lock conflicts with ownership transfers that take the same row
    ``FOR UPDATE``, so a concurrent transfer (or account disable) cannot commit
    between the authority check and the caller's final write commit.
    """

    from sqlalchemy.exc import DBAPIError

    try:
        accounts = await IdentityAccountLockData(db).lock_accounts(
            [actor_id] if actor_id is not None else [], mode="share", nowait=nowait
        )
        if db.autoflush:
            await db.flush()
        reference = await db.scalar(
            _LOCK_PROJECT_REFERENCE_NOWAIT if nowait else _LOCK_PROJECT_REFERENCE,
            {"project_id": project_id},
        )
        if reference is not None and (
            not isinstance(reference, uuid.UUID) or reference != project_id
        ):
            raise HTTPException(
                status_code=503,
                detail="Workspace Project reference lock unavailable",
            )
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from exc
        raise
    return accounts.get(actor_id)


async def lock_project_manager(
    db: AsyncSession,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    denial_detail: str = "项目管理权限已变化",
) -> None:
    """Serialize current management authority with account/ownership changes."""
    resolver = bind_project_write_policy_resolver(db)
    try:
        access = await resolver.resolve(
            ProjectWritePolicyRequest(actor_id=actor_id, project_id=project_id)
        )
    except ProjectWritePolicyFailure as exc:
        restore_project_write_policy_failure(exc)
    if not access.is_manager:
        raise HTTPException(status_code=403, detail=denial_detail)


async def assert_phase_write_allowed(
    db: AsyncSession,
    task: Task,
    actor: User | IdentityActorFacts | IdentityActorStatus,
    *,
    action: str = "write",
    lock_membership: bool = True,
) -> None:
    """Authorize one final write to ``task`` by ``actor``.

    Raises 403/409 through the canonical helpers; callers translate them into
    their worker's terminal failure state.
    """

    from app.services import task_access
    from app.services.annotation_evidence import assert_review_evidence_current
    from app.services.project_access import resolve_project_access

    project = await get_workspace_project(
        db, actor_id=actor.id, project_id=task.project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="task_missing")
    try:
        access = await resolve_project_access(
            db, actor_id=actor.id, project=project, lock_membership=lock_membership
        )
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="permission_changed") from exc

    task_access.assert_task_editable(task, actor.id, access=access)
    effective_annotator_id = await task_access.resolve_effective_task_assignee_id(
        db, task
    )
    if task.status == "review":
        assert_review_evidence_current(
            task, actor.id, effective_annotator_id=effective_annotator_id
        )
        return
    task_access.assert_effective_task_assignee(
        actor.id,
        effective_annotator_id,
        action=action,
        project_id=task.project_id,
        access=access,
    )
