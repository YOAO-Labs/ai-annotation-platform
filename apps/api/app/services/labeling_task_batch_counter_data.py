"""Persist the source-less Batch counter snapshot inside its caller's flush."""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, attributes
from sqlalchemy.orm.exc import StaleDataError

from aap_labeling.task_maintenance_contract import (
    TASK_MAINTENANCE_PROTOCOL,
    BatchCounterPatch,
    TaskMaintenanceUnavailable,
    WriteSourceLessBatchCounters,
    decode_task_maintenance_reply,
    task_maintenance_command_json,
)
from app.db.models.task_batch import TaskBatch
from app.services.labeling_annotation_data import _native_error_fields

_PENDING_BATCHES = "aap_labeling_source_less_batch_counter_pending"
_LISTENERS_INSTALLED = "aap_labeling_source_less_batch_counter_listeners"
_COUNTER_FIELDS = ("total_tasks", "completed_tasks", "review_tasks")
_COUNTER_FIELD_SET = frozenset(_COUNTER_FIELDS)
_WRITE_BATCH_COUNTERS = text(
    "SELECT aap_labeling_v1.task_maintenance_command(CAST(:request AS jsonb))"
)


def register_source_less_batch_counter_projection(
    db, project_id: uuid.UUID, batch: TaskBatch
) -> None:
    """Opt one loaded Batch into the next flush on this Session only."""
    session = db.sync_session
    pending = session.info.setdefault(_PENDING_BATCHES, {})
    pending.setdefault(project_id, {})[batch.id] = batch
    if session.info.get(_LISTENERS_INSTALLED):
        return
    event.listen(session, "before_flush", _write_pending_batch_counters)
    event.listen(session, "after_soft_rollback", _clear_pending_batch_counters)
    event.listen(session, "after_transaction_end", _clear_finished_batch_counter_scope)
    session.info[_LISTENERS_INSTALLED] = True


def _discard_batch(
    session: Session, project_id: uuid.UUID, batch_id: uuid.UUID
) -> None:
    pending = session.info.get(_PENDING_BATCHES)
    if not pending:
        return
    project_batches = pending.get(project_id)
    if project_batches is None:
        return
    project_batches.pop(batch_id, None)
    if not project_batches:
        pending.pop(project_id, None)
    if not pending:
        session.info.pop(_PENDING_BATCHES, None)


def _changed_batch_patch(
    project_id: uuid.UUID, batch: TaskBatch
) -> tuple[BatchCounterPatch | None, dict[str, int], bool, bool]:
    state = attributes.instance_state(batch)
    changed = {
        name: getattr(batch, name)
        for name in _COUNTER_FIELDS
        if state.attrs[name].history.has_changes()
    }
    if not changed:
        return None, {}, False, False
    batch_id = state.identity[0] if state.identity else None
    if not isinstance(batch_id, uuid.UUID) or batch.id != batch_id:
        raise StaleDataError(
            "UPDATE statement on table 'task_batches' expected to update 1 row(s); 0 were matched."
        )
    explicit_updated_at = state.attrs.updated_at.history.has_changes()
    updated_at = batch.updated_at if explicit_updated_at else None
    patch = BatchCounterPatch(
        batch_id=batch_id,
        total_tasks_set="total_tasks" in changed,
        total_tasks=batch.total_tasks if "total_tasks" in changed else None,
        completed_tasks_set="completed_tasks" in changed,
        completed_tasks=(
            batch.completed_tasks if "completed_tasks" in changed else None
        ),
        review_tasks_set="review_tasks" in changed,
        review_tasks=batch.review_tasks if "review_tasks" in changed else None,
        updated_at_set=explicit_updated_at,
        updated_at=updated_at,
    )
    other_dirty_fields = any(
        name not in _COUNTER_FIELD_SET and name != "updated_at"
        for name in state.committed_state
    )
    return patch, changed, explicit_updated_at, other_dirty_fields


def _owner_error(exc: DBAPIError) -> None:
    owner_message, detail, sqlstate = _native_error_fields(exc)
    if owner_message != "aap_labeling_task_maintenance_error":
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise HTTPException(409, detail={"reason": "batch_busy"}) from exc
        raise exc
    try:
        reason = json.loads(detail or "{}").get("reason")
    except (ValueError, TypeError):
        reason = None
    if reason == "batch_target_changed":
        raise StaleDataError(
            "UPDATE statement on table 'task_batches' expected to update 1 row(s); 0 were matched."
        ) from exc
    if reason in {"task_busy", "batch_busy"} or sqlstate in {
        "55P03",
        "40P01",
        "40001",
    }:
        raise HTTPException(409, detail={"reason": "batch_busy"}) from exc
    raise HTTPException(503, detail={"reason": "task_maintenance_unavailable"}) from exc


def _write_pending_batch_counters(session: Session, flush_context, instances) -> None:
    pending = session.info.get(_PENDING_BATCHES)
    if not pending:
        return
    connection = session.connection()
    for project_id, project_batches in list(pending.items()):
        targets = dict(project_batches)
        patches: list[BatchCounterPatch] = []
        changed_by_id: dict[
            uuid.UUID, tuple[TaskBatch, dict[str, int], bool, bool]
        ] = {}
        for batch_id, batch in targets.items():
            patch, changed, explicit_updated_at, other_dirty_fields = (
                _changed_batch_patch(project_id, batch)
            )
            if patch is None:
                _discard_batch(session, project_id, batch_id)
                continue
            patches.append(patch)
            changed_by_id[batch_id] = (
                batch,
                changed,
                explicit_updated_at,
                other_dirty_fields,
            )
        if not patches:
            continue

        request = WriteSourceLessBatchCounters(
            schema=TASK_MAINTENANCE_PROTOCOL,
            operation="write_source_less_batch_counters",
            project_id=project_id,
            batches=patches,
        )
        try:
            raw = connection.execute(
                _WRITE_BATCH_COUNTERS,
                {"request": task_maintenance_command_json(request)},
            ).scalar_one()
            data = decode_task_maintenance_reply(request, raw)
        except DBAPIError as exc:
            _owner_error(exc)
        except TaskMaintenanceUnavailable as exc:
            raise HTTPException(
                503, detail={"reason": "task_maintenance_unavailable"}
            ) from exc

        snapshots = {snapshot.batch_id: snapshot for snapshot in data.batches}
        if snapshots.keys() != changed_by_id.keys():
            raise HTTPException(503, detail={"reason": "task_maintenance_unavailable"})
        for batch_id, (
            batch,
            changed,
            explicit_updated_at,
            other_dirty_fields,
        ) in changed_by_id.items():
            snapshot = snapshots[batch_id]
            for name, value in changed.items():
                attributes.set_committed_value(batch, name, value)
            if explicit_updated_at and not other_dirty_fields:
                attributes.set_committed_value(batch, "updated_at", snapshot.updated_at)
            elif not explicit_updated_at and not other_dirty_fields:
                session.expire(batch, ["updated_at"])
            _discard_batch(session, project_id, batch_id)


def _clear_pending_batch_counters(session: Session, previous_transaction) -> None:
    session.info.pop(_PENDING_BATCHES, None)


def _clear_finished_batch_counter_scope(session: Session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_PENDING_BATCHES, None)
