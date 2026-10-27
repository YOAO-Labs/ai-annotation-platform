"""Current-operation authority for accepting content-qualified image candidates."""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from app.services import task_access
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.dataset_references import reference_snapshot, reference_context
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.labeling_core_read_relations import (
    batch_row,
    get_core_row,
    lock_task_for_core_read,
    prediction_meta_row,
    prediction_row,
    task_row,
)
from app.services.project_access import get_workspace_project
from app.services.prediction import to_internal_shape
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import (
    task_binding,
    reference_binding,
    lock_reference_bindings,
    normalize_binding,
)
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.task_lock import TaskLockService, TaskLockConflictError
from app.services.task_scope import lock_task_scope


_ISSUER = object()
_GEOMETRY = {"bbox", "rotated_bbox", "polygon", "multi_polygon", "polyline", "keypoint"}


async def _lock_edit_scope(db, task_id):
    try:
        await TaskLockService(db)._lock_task_scope(task_id, nowait=True)
    except TaskLockConflictError:
        raise ContentError("prediction_acceptance_busy") from None


@dataclass(frozen=True, slots=True)
class _AcceptanceReceipt:
    issuer: object
    session: object
    transaction: object
    actor_id: object
    task_id: object
    prediction_id: object
    binding: str
    candidate: str


def _fingerprint(prediction, metadata):
    payload = {
        "id": str(prediction.id),
        "created_at": str(prediction.created_at),
        "task_id": str(prediction.task_id),
        "project_id": str(prediction.project_id),
        "source": prediction.source,
        "tool_unit_id": prediction.tool_unit_id,
        "result": prediction.result,
        "rejected": prediction.rejected_shape_indexes,
        "metadata": metadata.extra if metadata else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


async def _candidate(db, task_id, prediction_id, *, lock=False):
    if lock:
        from app.services.labeling_core_read_relations import (
            lock_prediction_for_core_read_share,
        )

        await lock_prediction_for_core_read_share(db, prediction_id, task_id=task_id)
    query = select(prediction_row).where(
        prediction_row.id == prediction_id, prediction_row.task_id == task_id
    )
    prediction = await db.scalar(query.execution_options(populate_existing=True))
    if prediction is None:
        raise HTTPException(404, detail="Prediction not found")
    query = select(prediction_meta_row).where(
        prediction_meta_row.prediction_id == prediction.id
    )
    metadata_row = await db.scalar(query.execution_options(populate_existing=True))
    if lock and metadata_row is not None:
        from app.services.labeling_core_read_relations import (
            lock_prediction_meta_for_core_read_share,
        )

        await lock_prediction_meta_for_core_read_share(db, metadata_row.id)
    metadata = metadata_row
    return prediction, metadata


async def _check_content(db, task, binding, prediction, metadata, *, final=False):
    evidence = (metadata.extra or {}).get("reference_input") if metadata else None
    if not isinstance(evidence, dict) or evidence.get("schema") != 1:
        raise ContentError("prediction_requires_reference_provenance")
    if (
        evidence.get("item_id") != str(task.dataset_item_id)
        or prediction.project_id != task.project_id
        or any(
            evidence.get(key, 1 if key == "maintenance_revision" else None)
            != binding["reference"].get(key)
            for key in (
                "dataset_id",
                "asset_id",
                "sha256",
                "size",
                "width",
                "height",
                "maintenance_revision",
            )
        )
    ):
        raise ContentError("content_binding_changed")
    shapes = prediction.result or []
    if prediction.source == "mask_repair":
        if 0 in (prediction.rejected_shape_indexes or []):
            raise ContentError("repair_candidate_rejected")
        if (
            len(shapes) != 1
            or to_internal_shape(deepcopy(shapes[0])).get("geometry", {}).get("type")
            != "raster_mask"
        ):
            raise ContentError("reference_prediction_geometry_not_supported")
        from app.services.reference_mask_repair import qualify_repair_candidate

        await qualify_repair_candidate(
            db, task, binding, prediction, metadata, final=final
        )
        return
    if metadata and (metadata.extra or {}).get("reference_native") is not None:
        from app.services.reference_native_predictions import qualify_candidate

        await qualify_candidate(db, task, binding, prediction, metadata, final=final)
        return
    for shape in shapes:
        geometry = to_internal_shape(deepcopy(shape)).get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in _GEOMETRY:
            raise ContentError("reference_prediction_geometry_not_supported")


async def _authorize(db, task, actor):
    from app.services.project_write_guard import assert_phase_write_allowed

    fresh = await IdentityAccountLockData(db).read_actor_status(actor.id)
    project = await get_workspace_project(
        db, actor_id=actor.id, project_id=task.project_id, populate_existing=True
    )
    if (
        not fresh
        or not fresh.is_active
        or not project
        or project.data_type != "image"
        or task.file_type != "image"
    ):
        raise ContentError("prediction_acceptance_authority_revoked", 403)
    access = await task_access.resolve_task_access(
        db, task, fresh.id, lock_membership=False
    )
    task_access.assert_annotation_write_allowed(task, access)
    await task_access.assert_task_visible(db, task, fresh.id, access=access)
    task_access.assert_task_editable(task, fresh.id, access=access)
    await assert_phase_write_allowed(
        db, task, fresh, action="accept prediction", lock_membership=False
    )


async def qualify_acceptance(db, task, actor, request, prediction_id):
    """Capture outside lifecycle locks; hold the final proof until caller commits."""
    if db.sync_session.in_nested_transaction():
        raise ContentError("prediction_acceptance_receipt_invalid")
    prediction, metadata = await _candidate(db, task.id, prediction_id)
    await _authorize(db, task, actor)
    key_id = getattr(request.state, "api_key_id", None)
    keys = [
        await key_authority(db, actor, key_id, scope)
        for scope in ("tasks:read", "annotations:write")
    ]
    binding = {**task_binding(task), "reference": await reference_binding(db, task)}
    await _check_content(db, task, binding, prediction, metadata)

    fingerprint = _fingerprint(prediction, metadata)
    context = await reference_context(
        db, task.dataset_item_id, actor, task_id=task.id, task_authorizer=_authorize
    )
    await db.commit()
    snapshot = await reference_snapshot(
        db,
        task.dataset_item_id,
        actor,
        task_id=task.id,
        expected_sha256=binding["reference"]["sha256"],
        api_key_id=key_id,
        scope="tasks:read",
        task_authorizer=_authorize,
    )
    try:
        ref = binding["reference"]
        if (snapshot.size, snapshot.width, snapshot.height) != (
            ref["size"],
            ref["width"],
            ref["height"],
        ):
            raise ContentError("image_size_mismatch")
        await db.commit()
        await lock_actor_project_authority(db, actor.id, task.project_id)
        await lock_actor_scope(db, actor.id, task.project_id)
        await _lock_edit_scope(db, task.id)
        await lock_task_scope(db, [task.id])
        task = await lock_task_for_core_read(
            db, task.id, lock_mode="update", nowait=True, populate_existing=True
        )
        if task is None or not is_reference(task.file_path):
            raise ContentError("content_binding_changed")
        if task.batch_id is not None:
            await get_core_row(db, batch_row, task.batch_id, populate_existing=True)
        prediction, metadata = await _candidate(db, task.id, prediction_id, lock=True)
        await lock_reference_bindings(db, [binding], api_node_id())
        await _authorize(db, task, actor)
        if any(
            lock.user_id != actor.id
            for lock in await TaskLockService(db).active_locks(task.id, cleanup=False)
        ):
            raise ContentError("prediction_acceptance_task_locked")
        current = {**task_binding(task), "reference": await reference_binding(db, task)}
        if (
            current != binding
            or await reference_context(
                db,
                task.dataset_item_id,
                actor,
                task_id=task.id,
                task_authorizer=_authorize,
            )
            != context
            or _fingerprint(prediction, metadata) != fingerprint
        ):
            raise ContentError("content_binding_changed")
        if keys != [
            await key_authority(db, actor, key_id, scope)
            for scope in ("tasks:read", "annotations:write")
        ]:
            raise ContentError("api_key_authority_revoked", 403)
        await _check_content(db, task, binding, prediction, metadata, final=True)
        return _AcceptanceReceipt(
            _ISSUER,
            db.sync_session,
            db.sync_session.get_transaction(),
            actor.id,
            task.id,
            prediction.id,
            json.dumps(binding, sort_keys=True),
            fingerprint,
        )
    finally:
        snapshot.close()


async def validate_acceptance(db, prediction, actor_id, receipt):
    if (
        type(receipt) is not _AcceptanceReceipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.actor_id != actor_id
        or receipt.task_id != prediction.task_id
        or receipt.prediction_id != prediction.id
    ):
        raise ContentError("prediction_acceptance_receipt_invalid")
    task = await get_core_row(db, task_row, prediction.task_id, populate_existing=True)
    if not task or not is_reference(task.file_path):
        raise ContentError("prediction_acceptance_receipt_invalid")
    prediction, metadata = await _candidate(db, task.id, prediction.id)
    binding = {**task_binding(task), "reference": await reference_binding(db, task)}
    if (
        binding != normalize_binding(json.loads(receipt.binding))
        or _fingerprint(prediction, metadata) != receipt.candidate
    ):
        raise ContentError("prediction_acceptance_receipt_invalid")
    await _check_content(db, task, binding, prediction, metadata)


async def qualify_rejection(db, task, actor, request, prediction_id):
    """Reject owned candidates under current write authority without original I/O."""
    key_id = getattr(request.state, "api_key_id", None)
    scopes = ("tasks:read", "annotations:write")
    proof = [await key_authority(db, actor, key_id, scope) for scope in scopes]
    await lock_actor_project_authority(db, actor.id, task.project_id)
    await lock_actor_scope(db, actor.id, task.project_id)
    await _lock_edit_scope(db, task.id)
    await lock_task_scope(db, [task.id])
    task = await lock_task_for_core_read(
        db, task.id, lock_mode="update", nowait=True, populate_existing=True
    )
    if task is None or not is_reference(task.file_path):
        raise ContentError("content_binding_changed")
    await _authorize(db, task, actor)
    if any(
        lock.user_id != actor.id
        for lock in await TaskLockService(db).active_locks(task.id, cleanup=False)
    ):
        raise ContentError("prediction_acceptance_task_locked")
    if proof != [await key_authority(db, actor, key_id, scope) for scope in scopes]:
        raise ContentError("api_key_authority_revoked", 403)
    await _candidate(db, task.id, prediction_id, lock=True)
