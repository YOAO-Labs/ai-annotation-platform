"""Qualify persisted repair candidates and read-only canonical image masks."""

from copy import deepcopy
import uuid

from sqlalchemy import select

from app.services.content_snapshot import ContentError
from app.services.mask_repair import canonical_digest, load_repair_batch_source
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    lock_annotation_rows_for_core_read,
)


_READ_KINDS = frozenset({"candidate_read", "annotation_read"})
_WRITE_KINDS = frozenset({"prediction", "mutation", "candidate_accept"})


def _new_plan():
    return ContentError("repair_original_requires_new_plan")


async def _authorize_read(db, task, actor):
    from app.services import task_access
    from app.services.identity_account_lock_data import IdentityAccountLockData

    actor = await IdentityAccountLockData(db).read_actor_status(actor.id)
    if actor is None or not actor.is_active:
        raise ContentError("prediction_acceptance_authority_revoked", 403)
    await task_access.assert_task_visible(db, task, actor.id)


async def _authorize_accept(db, task, actor):
    from app.services.prediction_acceptance import _authorize
    from app.services.task_lock import TaskLockService

    await _authorize(db, task, actor)
    if any(
        row.user_id != actor.id
        for row in await TaskLockService(db).active_locks(task.id, cleanup=False)
    ):
        raise ContentError("prediction_acceptance_task_locked")


async def _candidate(db, task_id, prediction_id, *, lock=False):
    if lock:
        from app.services.labeling_core_read_relations import (
            lock_prediction_for_core_read_share,
        )

        await lock_prediction_for_core_read_share(db, prediction_id, task_id=task_id)
    from app.services.labeling_core_read_relations import (
        prediction_meta_row,
        prediction_row,
    )

    query = select(prediction_row).where(
        prediction_row.id == prediction_id, prediction_row.task_id == task_id
    )
    prediction = await db.scalar(query.execution_options(populate_existing=True))
    if prediction is None:
        raise ContentError("repair_candidate_provenance_invalid")
    query = select(prediction_meta_row).where(
        prediction_meta_row.prediction_id == prediction.id
    )
    metadata = await db.scalar(query.execution_options(populate_existing=True))
    if lock and metadata is not None:
        from app.services.labeling_core_read_relations import (
            lock_prediction_meta_for_core_read_share,
        )

        await lock_prediction_meta_for_core_read_share(db, metadata.id)
    return prediction, metadata


def _identity(evidence):
    from app.services.managed_image_inference import _FIELDS, normalize_identity

    if not isinstance(evidence, dict):
        raise _new_plan()
    return normalize_identity({key: evidence.get(key) for key in _FIELDS})


async def _qualified_candidate(
    db, task, prediction, metadata, *, lock=False, accepting=False
):
    """Check durable unit ownership; no original, model or object-store I/O."""
    from app.services.managed_mask_repair import annotation_facts
    from app.services.prediction import to_internal_shape

    extra = metadata.extra if metadata is not None else None
    info = extra.get("mask_repair") if isinstance(extra, dict) else None
    evidence = extra.get("original_input") if isinstance(extra, dict) else None
    if not isinstance(evidence, dict):
        raise _new_plan()
    if (
        prediction.source != "mask_repair"
        or prediction.project_id != task.project_id
        or not isinstance(info, dict)
        or extra.get("reference_input") is not None
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    identity = _identity(evidence)
    try:
        batch_id = uuid.UUID(info["batch_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ContentError("repair_candidate_provenance_invalid") from None
    batch = await load_repair_batch_source(db, batch_id=batch_id, lock=lock)
    if (
        batch is None
        or batch.project_id != task.project_id
        or evidence.get("repair_id", evidence.get("batch_id")) != str(batch.id)
        or evidence.get("plan_digest") != batch.plan_digest
        or canonical_digest(batch.plan_json) != batch.plan_digest
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    matches = [
        (index, item)
        for index, item in enumerate((batch.plan_json or {}).get("items") or [])
        if item.get("kind") == "rerun_local_sam"
        and item.get("task_id") == str(task.id)
        and item.get("issue_id") == info.get("issue_id")
        and item.get("annotation_id") == info.get("source_annotation_id")
        and item.get("source_version") == info.get("source_version")
    ]
    if len(matches) != 1:
        raise ContentError("repair_candidate_provenance_invalid")
    index, item = matches[0]
    expected = item.get("original_input")
    if not isinstance(expected, dict) or not isinstance(expected.get("original"), dict):
        raise _new_plan()
    from app.services.managed_mask_repair import public_identity

    if identity != public_identity(expected, identity["width"], identity["height"]):
        raise ContentError("content_binding_changed")
    model = item.get("original_model")
    if (
        not isinstance(model, dict)
        or info.get("model_id") != model.get("model_id")
        or not isinstance(evidence.get("model"), dict)
        or any(
            evidence["model"].get(key) != model.get(key)
            for key in (
                "backend_id",
                "pool_id",
                "model_id",
                "model_digest",
                "identity_guarantee",
            )
        )
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    unit = ((batch.result_json or {}).get("candidates") or {}).get(str(index)) or {}
    identifiers = unit.get("prediction_ids") or []
    candidate_index = info.get("candidate_index")
    record = (unit.get("candidate_records") or {}).get(str(prediction.id)) or {}
    if (
        unit.get("status") != "completed"
        or unit.get("kind") != "rerun_local_sam"
        or type(candidate_index) is not int
        or not 0 <= candidate_index < len(identifiers)
        or identifiers[candidate_index] != str(prediction.id)
        or unit.get("candidate_count") != len(identifiers)
        or record.get("result_digest") != canonical_digest(prediction.result)
        or record.get("metadata_digest") != canonical_digest(extra)
        or len(prediction.result or []) != 1
        or to_internal_shape(deepcopy(prediction.result[0]))
        .get("geometry", {})
        .get("type")
        != "raster_mask"
        or 0 in (prediction.rejected_shape_indexes or [])
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    parent_id = uuid.UUID(item["annotation_id"])
    if lock:
        parents = await lock_annotation_rows_for_core_read(
            db, [parent_id], lock_mode="share", nowait=True, populate_existing=True
        )
        parent = parents[0] if parents else None
    else:
        parent = await get_core_row(
            db, annotation_row, parent_id, populate_existing=True
        )
    if (
        parent is None
        or parent.task_id != task.id
        or not parent.is_active
        or parent.was_cancelled
        or parent.is_locked
        or int(parent.version or 1) != item["source_version"]
        or canonical_digest(parent.geometry) != item.get("source_geometry_digest")
        or annotation_facts(parent) != item.get("original_annotation")
    ):
        raise ContentError("repair_candidate_source_changed")
    if accepting:
        accepted = await db.scalar(
            select(annotation_row.id)
            .where(
                annotation_row.task_id == task.id,
                annotation_row.parent_prediction_id == prediction.id,
            )
            .limit(1)
        )
        if accepted is not None:
            raise ContentError("repair_candidate_already_accepted")
    return (
        expected,
        identity,
        {
            "batch_id": str(batch.id),
            "plan_digest": batch.plan_digest,
            "unit_digest": canonical_digest([item]),
            "prediction_id": str(prediction.id),
        },
    )


def _candidate_payload(prediction, metadata, selection=None):
    from app.services.prediction import to_internal_shape

    return {
        "prediction_id": str(prediction.id),
        "result_digest": canonical_digest(prediction.result),
        "metadata_digest": canonical_digest(metadata.extra),
        "geometry": to_internal_shape(deepcopy(prediction.result[0]))["geometry"],
        "selection": deepcopy(selection),
    }


async def qualify_acceptance(db, task, actor, request, prediction_id):
    """A new caller qualifies a stored candidate with its own write authority."""
    from app.services.managed_mask_repair import ManagedRepairSource

    try:
        selection = await request_selection(request)
    except (ValueError, TypeError):
        # The route schema retains its ordinary 422 responses; malformed input
        # never starts a source read or receives any private receipt.
        return None
    task_id = task.id
    prediction, metadata = await _candidate(db, task_id, prediction_id)
    expected, identity, context = await _qualified_candidate(
        db, task, prediction, metadata, accepting=True
    )
    await _authorize_accept(db, task, actor)
    owner = ManagedRepairSource(
        db,
        task,
        actor,
        request,
        expected=expected,
        authorize=_authorize_accept,
        scope="annotations:write",
    )
    try:
        await owner.capture()
        await owner.open_close()
        await owner.final()
        from app.services.labeling_core_read_relations import get_core_row, task_row

        task = await get_core_row(db, task_row, task_id, populate_existing=True)
        if task is None:
            raise ContentError("content_binding_changed")
        prediction, metadata = await _candidate(db, task_id, prediction_id, lock=True)
        _, current_identity, current_context = await _qualified_candidate(
            db, task, prediction, metadata, lock=True, accepting=True
        )
        if identity != current_identity or context != current_context:
            raise ContentError("repair_candidate_provenance_invalid")
        if owner.identity() != identity:
            raise ContentError("content_binding_changed")
        return owner.receipt(
            "candidate_accept",
            _candidate_payload(prediction, metadata, selection),
            context=context,
        )
    except BaseException as exc:
        await owner.close(exc)
        raise


async def request_selection(request):
    query = request.query_params
    value = query.get("shape_index")
    try:
        index = None if value is None else int(value)
    except (ValueError, TypeError):
        raise ValueError("invalid shape index") from None
    if index is not None and index < 0:
        raise ValueError("invalid shape index")
    raw = await request.body()
    body = await request.json() if raw else None
    if body is not None and not isinstance(body, dict):
        raise ValueError("invalid acceptance body")
    attributes = body.get("attribute_overrides") if isinstance(body, dict) else None
    if attributes is not None and not isinstance(attributes, dict):
        raise ValueError("invalid attribute overrides")
    return selection_value(
        index,
        query.get("override_class_name"),
        attributes,
        query.get("video_segment_id"),
    )


def selection_value(
    shape_index=None,
    override_class_name=None,
    attribute_overrides=None,
    video_segment_id=None,
):
    return {
        "shape_index": shape_index,
        "override_class_name": override_class_name,
        "attribute_overrides": deepcopy(attribute_overrides),
        "video_segment_id": str(uuid.UUID(str(video_segment_id)))
        if video_segment_id is not None
        else None,
    }


async def validate_acceptance(db, prediction, actor_id, receipt, *, selection=None):
    from app.services.managed_mask_repair import receipt_payload, validate_receipt

    kind, payload = receipt_payload(receipt)
    if kind != "candidate_accept":
        raise ContentError("repair_original_receipt_invalid")
    from app.services.labeling_core_read_relations import get_core_row, task_row

    task = await get_core_row(db, task_row, prediction.task_id, populate_existing=True)
    if task is None:
        raise ContentError("repair_original_receipt_invalid")
    prediction, metadata = await _candidate(db, task.id, prediction.id, lock=True)
    await _qualified_candidate(
        db, task, prediction, metadata, lock=True, accepting=True
    )
    if payload != _candidate_payload(prediction, metadata, selection):
        raise ContentError("repair_original_receipt_invalid")
    return await validate_receipt(
        db, task.id, actor_id, receipt, kind, payload, project_id=task.project_id
    )


async def qualify_candidate_read(db, task, actor, request, prediction, metadata):
    from app.services.managed_mask_repair import ManagedRepairSource

    task_id, prediction_id = task.id, prediction.id
    expected, identity, context = await _qualified_candidate(
        db, task, prediction, metadata
    )
    owner = ManagedRepairSource(
        db,
        task,
        actor,
        request,
        expected=expected,
        authorize=_authorize_read,
        scope="annotations:read",
    )
    try:
        await owner.capture()
        await owner.open_close()
        await owner.final()
        from app.services.labeling_core_read_relations import get_core_row, task_row

        task = await get_core_row(db, task_row, task_id, populate_existing=True)
        if task is None:
            raise ContentError("content_binding_changed")
        prediction, metadata = await _candidate(db, task_id, prediction_id, lock=True)
        _, current_identity, current_context = await _qualified_candidate(
            db, task, prediction, metadata, lock=True
        )
        if (
            current_identity != identity
            or current_context != context
            or owner.identity() != identity
        ):
            raise ContentError("content_binding_changed")
        return owner.receipt(
            "candidate_read", _candidate_payload(prediction, metadata), context=context
        )
    except BaseException as exc:
        await owner.close(exc)
        raise


async def qualify_annotation_read(db, task, actor, request, annotation):
    """Read existing native/repair masks without granting any write capability."""
    from app.services.managed_mask_repair import ManagedRepairSource, annotation_facts

    task_id, annotation_id, parent_id = (
        task.id,
        annotation.id,
        annotation.parent_prediction_id,
    )
    frozen = annotation_facts(annotation)
    payload = {
        "annotation_id": str(annotation_id),
        "geometry": deepcopy(annotation.geometry),
    }
    owner = ManagedRepairSource(
        db,
        task,
        actor,
        request,
        expected=None,
        authorize=_authorize_read,
        scope="annotations:read",
    )
    try:
        await owner.capture()
        await owner.open_close()
        await owner.final()
        from app.services.labeling_core_read_relations import get_core_row, task_row

        task = await get_core_row(db, task_row, task_id, populate_existing=True)
        if task is None:
            raise ContentError("content_binding_changed")
        if parent_id:
            prediction, metadata = await _candidate(db, task_id, parent_id, lock=True)
            evidence = (
                (metadata.extra or {}).get("original_input") if metadata else None
            )
            if prediction.source == "mask_repair" and evidence is None:
                raise _new_plan()
            if evidence is not None and owner.identity() != _identity(evidence):
                raise ContentError("content_binding_changed")
        from app.services.labeling_core_read_relations import (
            annotation_row,
            lock_annotation_rows_for_core_read,
        )

        await lock_annotation_rows_for_core_read(
            db,
            [annotation_id],
            task_id=task.id,
            lock_mode="share",
            nowait=True,
        )
        current = await db.scalar(
            select(annotation_row)
            .where(annotation_row.id == annotation_id)
            .execution_options(populate_existing=True)
        )
        if (
            current is None
            or current.task_id != task.id
            or not current.is_active
            or current.was_cancelled
            or annotation_facts(current) != frozen
        ):
            raise ContentError("repair_candidate_source_changed")
        return owner.receipt(
            "annotation_read", payload, context={"annotation_id": str(annotation_id)}
        )
    except BaseException as exc:
        await owner.close(exc)
        raise


async def validate_geometry_receipt(db, task, receipt, geometry, *, write):
    """Scope dimensions to an exact geometry in a privately issued operation."""
    from app.services.managed_mask_repair import receipt_payload, validate_receipt
    from app.services.raster_mask_storage import (
        collect_mask_geometries,
        validate_image_mask_dimensions,
    )

    kind, payload = receipt_payload(receipt)
    if kind not in (_WRITE_KINDS if write else _READ_KINDS):
        raise ContentError("repair_original_receipt_invalid")
    value = payload.get("mutations", []) if kind == "mutation" else payload
    if not any(row == geometry for row in collect_mask_geometries(value)):
        raise ContentError("repair_original_receipt_invalid")
    evidence = await validate_receipt(
        db, task.id, None, receipt, kind, payload, project_id=task.project_id
    )
    validate_image_mask_dimensions(geometry, evidence["width"], evidence["height"])
    return evidence
