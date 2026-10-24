"""Qualify managed native Masks with a private, transaction-bound original receipt."""

from dataclasses import dataclass, field
import json

from sqlalchemy import select

from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services import prediction_originals as originals
from app.services.project_access import resolve_project_access
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id

_ISSUER = object()


@dataclass(frozen=True, slots=True)
class _NativeReceipt:
    issuer: object
    session: object
    transaction: object
    actor_id: object
    task_id: object
    binding: str = field(repr=False)
    idempotency_key: object
    request_digest: str
    mask_digest: str
    evidence: str = field(repr=False)


async def qualify_native_acceptance(db, task, actor, request, data):
    from app.services.ai_mask_accept import (
        _validate_receipt,
        _validate_replay,
        ai_mask_accept_request_digest,
    )
    from app.services.managed_image_inference import (
        ManagedImageOperation,
        normalize_identity,
    )
    from app.services.raster_mask_storage import build_rle_reference

    if db.sync_session.in_nested_transaction():
        raise ContentError("prediction_original_receipt_invalid")
    mask = build_rle_reference(
        data.candidate.candidate.value.rle.model_dump(mode="json")
    )
    request_digest = ai_mask_accept_request_digest(data)
    query = (
        select(AiMaskAcceptDecision)
        .where(
            AiMaskAcceptDecision.task_id == task.id,
            AiMaskAcceptDecision.idempotency_key == data.idempotency_key,
        )
        .execution_options(populate_existing=True)
    )
    existing = await db.scalar(query)
    # A completed Decision has its own lifetime. Its exact request and current
    # original must still match, but an expired candidate need not be re-signed.
    claims = (
        None
        if existing
        else _validate_receipt(data, task_id=task.id, content_digest=mask["sha256"])
    )
    owner = ManagedImageOperation(
        db,
        task,
        actor,
        request,
        "ai_mask_accept",
        prompt_source=claims.get("prompt_source") if claims else None,
    )
    failure = None
    try:
        await owner.open_for_acceptance(annotations_read=data.target.mode == "refine")
        # No source handle or permit remains open while final business locks are held.
        await owner.final()
        current = await db.scalar(query.with_for_update(read=True, nowait=True))
        if current:
            project = await db.get(Project, task.project_id, populate_existing=True)
            access = await resolve_project_access(db, user=actor, project=project)
            _validate_replay(current, request_digest, actor, access=access)
            evidence = (current.response_json or {}).get("_original_input")
            if (current.response_json or {}).get("_reference_input") is not None:
                raise ContentError("prediction_original_receipt_invalid")
            origin = (
                {name: evidence.get(name) for name in owner.identity()}
                if isinstance(evidence, dict)
                else None
            )
        elif existing:
            raise ContentError("prediction_original_receipt_invalid")
        else:
            if claims.get("reference_input") is not None:
                raise ContentError("prediction_original_receipt_invalid")
            origin = claims.get("original_input")
        if origin is None:
            raise ContentError("prediction_requires_original_provenance")
        origin = normalize_identity(origin)
        if origin != owner.identity():
            raise ContentError("prediction_requires_original_provenance")
        return _NativeReceipt(
            _ISSUER,
            db.sync_session,
            db.sync_session.get_transaction(),
            actor.id,
            task.id,
            originals.encoded(owner.binding),
            data.idempotency_key,
            request_digest,
            mask["sha256"],
            originals.encoded(
                {**origin, "actor_id": str(actor.id), "node_id": api_node_id()}
            ),
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await owner.close(failure)


async def validate_native_receipt(
    db, task_id, project_id, receipt, *, actor_id=None, data=None, result=None
):
    """Revalidate private authority without another physical read or transaction."""
    if (
        type(receipt) is not _NativeReceipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
        or (actor_id is not None and receipt.actor_id != actor_id)
    ):
        raise ContentError("prediction_original_receipt_invalid")
    task = await db.get(Task, task_id, populate_existing=True)
    if (
        task is None
        or task.project_id != project_id
        or task.file_type != "image"
        or is_reference(task.file_path)
    ):
        raise ContentError("prediction_original_receipt_invalid")
    binding = {
        **task_binding(task),
        "original": await originals.managed_binding(db, task, lock=True),
    }
    if originals.encoded(binding) != receipt.binding:
        raise ContentError("content_binding_changed")
    if data is not None:
        from app.services.ai_mask_accept import ai_mask_accept_request_digest

        if (
            data.idempotency_key != receipt.idempotency_key
            or ai_mask_accept_request_digest(data) != receipt.request_digest
        ):
            raise ContentError("prediction_original_receipt_invalid")
    evidence = json.loads(receipt.evidence)
    if result is not None:
        if (
            not isinstance(result, list)
            or len(result) != 1
            or not isinstance(result[0], dict)
        ):
            raise ContentError("prediction_original_geometry_not_supported")
        geometry = result[0].get("geometry") or {}
        if not isinstance(geometry, dict):
            raise ContentError("prediction_original_geometry_not_supported")
        mask = geometry.get("mask") or {}
        if (
            geometry.get("type") != "raster_mask"
            or not isinstance(mask, dict)
            or mask.get("sha256") != receipt.mask_digest
        ):
            raise ContentError("prediction_original_geometry_not_supported")
        from app.services.raster_mask_storage import validate_image_mask_dimensions

        validate_image_mask_dimensions(geometry, evidence["width"], evidence["height"])
    return evidence
