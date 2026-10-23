"""Keep trusted media seed writes on Workspace at the original flush points."""

from __future__ import annotations

from datetime import datetime
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased, attributes, object_session
from sqlalchemy.orm.exc import StaleDataError

from aap_workspace.media_seed_project_contract import (
    MediaSeedProjectUnavailable,
    MEDIA_SEED_PROJECT_PROJECTION_FIELDS,
    decode_media_seed_project_created,
    decode_seed_project_projection_write,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.labeling_project_counter_data import (
    register_project_counter_projection,
)
from app.services.workspace_read_relations import project_member_facts, project_rows

seed_projects = aliased(Project, project_rows(), adapt_on_names=True)
seed_members = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
_PENDING = "aap_workspace_seed_project_pending"
_EXPIRE_TIME = "aap_workspace_seed_project_expire_time"
_FIELDS = frozenset(MEDIA_SEED_PROJECT_PROJECTION_FIELDS)
_WRITE = text(
    "SELECT aap_workspace_v1.write_seed_project_projection(CAST(:project_id AS uuid), CAST(:patch AS jsonb))"
)


def _json_value(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("Seed Project timestamps must be timezone-aware")
        return value.isoformat()
    raise TypeError(f"Unsupported seed Project value type: {type(value).__name__}")


def _decode(raw, project_id):
    try:
        reply = decode_seed_project_projection_write(raw, project_id=project_id)
    except MediaSeedProjectUnavailable:
        raise HTTPException(status_code=503, detail="项目种子服务暂不可用") from None
    if not reply.found:
        raise StaleDataError("Seed Project UPDATE expected one row; none matched")
    return reply


def register_seed_project_projection(db: AsyncSession, project_id: uuid.UUID) -> None:
    db.sync_session.info.setdefault(_PENDING, set()).add(project_id)


async def create_media_seed_project(
    db: AsyncSession, *, kind, display_id, name, owner_id, tool_bindings
) -> uuid.UUID:
    project_id = uuid.uuid4()
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.create_media_seed_project("
            "CAST(:project_id AS uuid),CAST(:display_id AS text),CAST(:name AS text),"
            "CAST(:owner_id AS uuid),CAST(:kind AS text),CAST(:tool_bindings AS jsonb))"
        ),
        {
            "project_id": project_id,
            "display_id": display_id,
            "name": name,
            "owner_id": owner_id,
            "kind": kind,
            "tool_bindings": json.dumps(
                tool_bindings, ensure_ascii=True, allow_nan=False
            ),
        },
    )
    return decode_media_seed_project_created(
        raw, kind=kind, display_id=display_id, expected_project_id=project_id
    )


async def pin_seed_project_timestamps(
    db: AsyncSession, project: Project, *, created_at: datetime, updated_at: datetime
) -> None:
    patch = {"created_at": created_at, "updated_at": updated_at}
    raw = await db.scalar(
        _WRITE,
        {
            "project_id": project.id,
            "patch": json.dumps(patch, default=_json_value, allow_nan=False),
        },
    )
    _decode(raw, project.id)
    for name, value in patch.items():
        attributes.set_committed_value(project, name, value)


@event.listens_for(Project, "before_update")
def _write_seed_project(mapper, connection, target):
    session = object_session(target)
    identity = attributes.instance_state(target).identity
    if (
        session is None
        or not identity
        or identity[0] not in session.info.get(_PENDING, ())
    ):
        return
    project_id = identity[0]
    state = attributes.instance_state(target)
    patch = {
        name: getattr(target, name)
        for name in _FIELDS
        if state.attrs[name].history.has_changes()
    }
    if not patch:
        session.info[_PENDING].discard(project_id)
        return
    other_dirty = any(name not in _FIELDS for name in state.committed_state)
    explicit_time = "updated_at" in patch
    raw = connection.execute(
        _WRITE,
        {
            "project_id": project_id,
            "patch": json.dumps(
                patch, default=_json_value, ensure_ascii=True, allow_nan=False
            ),
        },
    ).scalar_one()
    _decode(raw, project_id)
    for name, value in patch.items():
        if name != "updated_at" or not other_dirty:
            attributes.set_committed_value(target, name, value)
    if not explicit_time:
        session.info.setdefault(_EXPIRE_TIME, set()).add(project_id)
    session.info[_PENDING].discard(project_id)


@event.listens_for(Project, "after_update")
def _expire_seed_project_timestamp(mapper, connection, target):
    session = object_session(target)
    identity = attributes.instance_state(target).identity
    if (
        session is not None
        and identity
        and identity[0] in session.info.get(_EXPIRE_TIME, ())
    ):
        session.info[_EXPIRE_TIME].discard(identity[0])
        session.expire(target, ["updated_at"])


@event.listens_for(Session, "after_soft_rollback")
def _clear_rolled_back_seed_scope(session, previous_transaction):
    session.info.pop(_PENDING, None)
    session.info.pop(_EXPIRE_TIME, None)


@event.listens_for(Session, "after_transaction_end")
def _clear_finished_seed_scope(session, transaction):
    if transaction.parent is None:
        session.info.pop(_PENDING, None)
        session.info.pop(_EXPIRE_TIME, None)


__all__ = [
    "create_media_seed_project",
    "pin_seed_project_timestamps",
    "register_seed_project_projection",
    "register_project_counter_projection",
    "seed_projects",
    "seed_members",
]
