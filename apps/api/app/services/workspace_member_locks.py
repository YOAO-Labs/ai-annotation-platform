"""Borrow caller transactions for fixed Workspace ProjectMember lock facts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
import uuid

from fastapi import HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_workspace_contract.member_values import (
    ProjectMemberRowsUnavailable,
    ProjectMemberRow,
    decode_project_member_rows,
)
from app.db.models.project_member import ProjectMember
from app.services.project_workspace import merge_project_snapshot

_LOCK_PROJECT_MEMBER_REFERENCE = text(
    "SELECT aap_workspace_v1.lock_project_member_reference("
    "CAST(:project_id AS uuid), CAST(:user_id AS uuid), CAST(:nowait AS boolean))"
)
_PROJECT_MEMBER_ROWS = text(
    "SELECT aap_workspace_v1.project_member_rows("
    "CAST(:project_id AS uuid), CAST(:user_ids AS uuid[]), "
    "CAST(:lock_members AS boolean))"
)


def _unavailable_member_rows() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Workspace ProjectMember rows unavailable",
    )


def _decode_project_member_rows(
    raw, *, project_id: uuid.UUID, requested_ids: Sequence[uuid.UUID]
) -> list[ProjectMemberRow]:
    try:
        rows = decode_project_member_rows(raw, target_id=project_id)
    except ProjectMemberRowsUnavailable:
        raise _unavailable_member_rows() from None

    user_ids = [row.user_id for row in rows]
    member_ids = [row.id for row in rows]
    requested = set(requested_ids)
    if (
        any(user_id not in requested for user_id in user_ids)
        or len(user_ids) != len(set(user_ids))
        or len(member_ids) != len(set(member_ids))
        or user_ids != sorted(user_ids)
    ):
        raise _unavailable_member_rows()
    return rows


def _expired_dirty_project_members(
    db: AsyncSession,
) -> dict[uuid.UUID, ProjectMember]:
    """Index only fully expired dirty identities without loading attributes."""
    members = {}
    for instance in db.sync_session.identity_map.values():
        if not isinstance(instance, ProjectMember):
            continue
        state = inspect(instance)
        if state.expired and state.modified and state.identity is not None:
            members[state.identity[0]] = instance
    return members


def _restore_unloaded_member_fields(instance, row) -> None:
    values = asdict(row)
    unloaded = inspect(instance).unloaded
    for key in values.keys() & unloaded:
        set_committed_value(instance, key, values[key])


async def lock_project_member_reference(
    db: AsyncSession,
    project_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    *,
    nowait: bool,
) -> None:
    """Hold a ProjectMember SHARE lock and validate the scalar owner fact."""
    if db.autoflush:
        await db.flush()
    reference = await db.scalar(
        _LOCK_PROJECT_MEMBER_REFERENCE,
        {"project_id": project_id, "user_id": user_id, "nowait": nowait},
    )
    if reference is not None and (
        not isinstance(reference, uuid.UUID) or reference != user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workspace ProjectMember reference lock unavailable",
        )


async def prime_expired_dirty_project_members(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_ids: Sequence[uuid.UUID],
) -> None:
    """Prime selected expired dirty identities before the caller's next flush."""
    if not db.autoflush:
        return

    requested_ids = tuple(sorted(set(user_ids)))
    expired_dirty = _expired_dirty_project_members(db)
    if expired_dirty:
        with db.no_autoflush:
            primer_raw = await db.scalar(
                _PROJECT_MEMBER_ROWS,
                {
                    "project_id": project_id,
                    "user_ids": list(requested_ids),
                    "lock_members": False,
                },
            )
        primer_rows = _decode_project_member_rows(
            primer_raw,
            project_id=project_id,
            requested_ids=requested_ids,
        )
        primer_by_id = {row.id: row for row in primer_rows}
        for member_id, instance in expired_dirty.items():
            row = primer_by_id.get(member_id)
            if row is not None:
                _restore_unloaded_member_fields(instance, row)


async def lock_project_members(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_ids: Sequence[uuid.UUID],
) -> list[ProjectMember]:
    """Wait-lock one ordered member set and refresh its caller identities."""
    requested_ids = tuple(sorted(set(user_ids)))
    await prime_expired_dirty_project_members(db, project_id, requested_ids)

    if db.autoflush:
        await db.flush()

    raw = await db.scalar(
        _PROJECT_MEMBER_ROWS,
        {
            "project_id": project_id,
            "user_ids": list(requested_ids),
            "lock_members": True,
        },
    )
    rows = _decode_project_member_rows(
        raw,
        project_id=project_id,
        requested_ids=requested_ids,
    )
    return [
        await merge_project_snapshot(db, ProjectMember, asdict(row)) for row in rows
    ]
