"""Persist Labeling-owned Project counters inside the caller's ORM flush."""

from __future__ import annotations

from datetime import datetime
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, attributes, object_session
from sqlalchemy.orm.exc import StaleDataError

from app.db.models.project import Project

_PENDING_PROJECT_IDS = "aap_labeling_project_counter_pending_ids"
_EXPIRE_UPDATED_AT_IDS = "aap_labeling_project_counter_expire_updated_at_ids"
_COUNTER_FIELDS = (
    "total_tasks",
    "completed_tasks",
    "review_tasks",
    "in_progress_tasks",
    "batch_summary",
)
_COUNTER_FIELD_SET = frozenset(_COUNTER_FIELDS)
_WRITE_COUNTER_PROJECTION = text(
    "SELECT aap_labeling_v1.write_project_counter_projection("
    "CAST(:project_id AS uuid), CAST(:patch AS jsonb))"
)

# Preserve the legacy flush points: Workspace reads may autoflush, while source-less cleanup may defer this write.


def _project_identity(target: Project) -> uuid.UUID | None:
    identity = attributes.instance_state(target).identity
    if not identity:
        return None
    project_id = identity[0]
    return project_id if isinstance(project_id, uuid.UUID) else None


def _discard_project_id(session: Session, info_key: str, project_id: uuid.UUID) -> None:
    project_ids = session.info.get(info_key)
    if not project_ids:
        return
    project_ids.discard(project_id)
    if not project_ids:
        session.info.pop(info_key, None)


def register_project_counter_projection(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Opt this Project into the scoped mapper bridge until its next flush."""
    db.sync_session.info.setdefault(_PENDING_PROJECT_IDS, set()).add(project_id)


def clear_project_counter_projection(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Drop an opt-in when the corresponding Project lookup found no row."""
    _discard_project_id(db.sync_session, _PENDING_PROJECT_IDS, project_id)


def _native_json_value(value):
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("Project updated_at must be timezone-aware")
        return value.isoformat()
    return value


@event.listens_for(Project, "before_update")
def _write_project_counters_before_update(mapper, connection, target: Project) -> None:
    session = object_session(target)
    project_id = _project_identity(target)
    if session is None or project_id is None:
        return
    if project_id not in session.info.get(_PENDING_PROJECT_IDS, ()):
        return

    state = attributes.instance_state(target)
    changed_counters = {
        name: getattr(target, name)
        for name in _COUNTER_FIELDS
        if state.attrs[name].history.has_changes()
    }
    if not changed_counters:
        _discard_project_id(session, _PENDING_PROJECT_IDS, project_id)
        return

    explicit_updated_at = state.attrs.updated_at.history.has_changes()
    patch = dict(changed_counters)
    if explicit_updated_at:
        patch["updated_at"] = _native_json_value(target.updated_at)

    # Preserve unrelated ORM edits. A dirty updated_at is left in the ORM
    # history only when another column still needs its ordinary Project UPDATE.
    other_dirty_fields = any(
        name not in _COUNTER_FIELD_SET and name != "updated_at"
        for name in state.committed_state
    )
    raw_project_id = connection.execute(
        _WRITE_COUNTER_PROJECTION,
        {
            "project_id": project_id,
            "patch": json.dumps(patch, ensure_ascii=True, allow_nan=False),
        },
    ).scalar_one()
    if raw_project_id is None:
        raise StaleDataError(
            "UPDATE statement on table 'projects' expected to update 1 row(s); "
            "0 were matched."
        )
    if not isinstance(raw_project_id, uuid.UUID) or raw_project_id != project_id:
        raise HTTPException(status_code=503, detail="标注项目计数服务暂不可用")

    for name, value in changed_counters.items():
        attributes.set_committed_value(target, name, value)
    if explicit_updated_at and not other_dirty_fields:
        attributes.set_committed_value(target, "updated_at", target.updated_at)
    elif not explicit_updated_at:
        session.info.setdefault(_EXPIRE_UPDATED_AT_IDS, set()).add(project_id)
    _discard_project_id(session, _PENDING_PROJECT_IDS, project_id)


@event.listens_for(Project, "after_update")
def _expire_implicit_counter_updated_at(mapper, connection, target: Project) -> None:
    session = object_session(target)
    project_id = _project_identity(target)
    if session is None or project_id is None:
        return
    if project_id not in session.info.get(_EXPIRE_UPDATED_AT_IDS, ()):
        return
    _discard_project_id(session, _EXPIRE_UPDATED_AT_IDS, project_id)
    session.expire(target, ["updated_at"])


@event.listens_for(Session, "after_soft_rollback")
def _clear_project_counter_flush_state(session: Session, previous_transaction) -> None:
    session.info.pop(_PENDING_PROJECT_IDS, None)
    session.info.pop(_EXPIRE_UPDATED_AT_IDS, None)


@event.listens_for(Session, "after_transaction_end")
def _clear_finished_project_counter_scope(session: Session, transaction) -> None:
    # close() may abandon pending work without dispatching after_soft_rollback.
    if transaction.parent is None:
        session.info.pop(_PENDING_PROJECT_IDS, None)
        session.info.pop(_EXPIRE_UPDATED_AT_IDS, None)
