"""Read-only current-caller qualification of owned whole-video mask content.

The server writes safe adoption lineage only from a private acceptance receipt.
Read receipts never grant a persistence capability. Requested RLE bytes and the
original are fully read and closed before final SQL locks; only one requested
mask is retained, including when a track contains thousands of other masks.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.services import prediction_originals as common
from app.services import storage_access, video_prediction_acceptance as accept
from app.services import video_prediction_originals as originals
from app.services import video_prediction_references as references
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.labeling_core_read_relations import prediction_meta_row
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.video_tracks import resolve_track_at_frame

LINEAGE_KEY = "_video_prediction"
_ISSUER = object()
_LINEAGE_FIELDS = frozenset(
    {
        "schema",
        "task_id",
        "project_id",
        "prediction_id",
        "prediction_created_at",
        "candidate_digest",
        "shape_index",
        "source_digest",
        "binding_digest",
        "probe",
        "geometry_digest",
    }
)


def _reject(code="prediction_video_read_receipt_invalid", status=409):
    raise ContentError(code, status)


def _binding_digest(binding):
    return common.digest(accept._without_version(binding))


def annotation_lineage(db, annotation, prediction, receipt, shape_index):
    """Issue safe, server-owned lineage; a client source/parent label is not proof."""
    receipt = accept._receipt(db, receipt)
    from app.services.prediction import to_internal_shape

    if (
        type(shape_index) is not int
        or shape_index not in receipt.selected
        or annotation.task_id != receipt.task_id
        or annotation.project_id != receipt.project_id
        or annotation.parent_prediction_id != receipt.prediction_id
        or prediction.id != receipt.prediction_id
        or prediction.task_id != receipt.task_id
        or prediction.project_id != receipt.project_id
        or annotation.source != "prediction_based"
        or not 0 <= shape_index < len(prediction.result)
        or not accept._same(
            annotation.geometry,
            to_internal_shape(deepcopy(prediction.result[shape_index])).get("geometry"),
        )
    ):
        _reject()
    evidence = json.loads(receipt.evidence)
    return accept._json(
        {
            "schema": 1,
            "task_id": str(receipt.task_id),
            "project_id": str(receipt.project_id),
            "prediction_id": str(receipt.prediction_id),
            "prediction_created_at": prediction.created_at.isoformat(),
            "candidate_digest": receipt.candidate,
            "shape_index": shape_index,
            "source_digest": common.digest(evidence["original"]),
            "binding_digest": _binding_digest(json.loads(receipt.binding)),
            "probe": evidence["probe"],
            "geometry_digest": common.digest(annotation.geometry),
        }
    )


def _lineage(annotation):
    metadata = annotation.attributes_meta
    value = metadata.get(LINEAGE_KEY) if isinstance(metadata, dict) else None
    if not isinstance(value, dict) or set(value) != _LINEAGE_FIELDS:
        _reject("prediction_video_requires_new_request")
    value = accept._json(value, limit=references.MAX_PROOF_BYTES)
    if (
        type(value["schema"]) is not int
        or value["schema"] != 1
        or value["task_id"] != str(annotation.task_id)
        or value["project_id"] != str(annotation.project_id)
        or type(value["shape_index"]) is not int
        or not 0 <= value["shape_index"] < accept.MAX_SHAPES
    ):
        _reject()
    references._uuid(value["prediction_id"])
    try:
        created_at = datetime.fromisoformat(value["prediction_created_at"])
        if created_at.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        _reject()
    for name in (
        "candidate_digest",
        "source_digest",
        "binding_digest",
        "geometry_digest",
    ):
        accept._sha(value[name])
    originals.normalize_probe(value["probe"])
    return value


async def requires_annotation_read(db, annotation):
    """Dispatch qualified masks while preserving old direct-item read paths."""
    if (
        isinstance(annotation.attributes_meta, dict)
        and LINEAGE_KEY in annotation.attributes_meta
    ):
        return True
    if annotation.parent_prediction_id is None:
        return False
    metadata_id = await db.scalar(
        select(prediction_meta_row.id).where(
            prediction_meta_row.prediction_id == annotation.parent_prediction_id
        )
    )
    metadata = (
        await accept._bounded_row(db, prediction_meta_row, metadata_id, ("extra",))
        if metadata_id is not None
        else None
    )
    extra = metadata.extra if metadata is not None else None
    return isinstance(extra, dict) and (
        accept.PROOF_KEY in extra or "original_video_input" in extra
    )


async def _credentials(db, value, actor_id):
    common.check_clock(value)
    key_id = uuid.UUID(value["api_key_id"]) if value["api_key_id"] else None
    actor = await common.reauthorize(db, value, actor_id, key_id)
    await key_authority(db, actor, key_id, "annotations:read")
    return actor


async def _current(db, task_id, project_id, actor_id, credentials, *, binding=None):
    from app.services import task_access

    await lock_actor_project_authority(db, actor_id, project_id)
    await lock_actor_scope(db, actor_id, project_id)
    actor = await _credentials(db, credentials, actor_id)
    task = await common.locked_task(db, task_id)
    if (
        task.project_id != project_id
        or task.file_type != "video"
        or is_reference(task.file_path)
    ):
        _reject("content_binding_changed")
    await task_access.assert_task_visible(db, task, actor.id)
    current = references._binding(
        {
            **task_binding(task),
            "original": await originals.managed_binding(db, task, lock=True),
        }
    )
    if binding is not None and not accept._same(current, binding):
        _reject("content_binding_changed")
    return task, current


async def _annotation(db, task_id, identifier, *, lock=False):
    row = await accept._bounded_row(
        db,
        Annotation,
        identifier,
        ("geometry", "attributes", "attributes_meta"),
        lock=lock,
    )
    if row is None or row.task_id != task_id or not row.is_active or row.was_cancelled:
        raise HTTPException(404, detail="annotation not found")
    return row


def _annotation_fingerprint(row):
    return common.digest(
        {
            column.key: getattr(row, column.key)
            for column in Annotation.__table__.columns
        }
    )


def _requested(geometry, probe, frame_index):
    if (
        geometry.get("type") != "video_track_mask"
        or type(frame_index) is not int
        or not 0 <= frame_index < probe["video"]["frame_count"]
    ):
        raise HTTPException(404, detail="mask is outside at this frame")
    resolved = resolve_track_at_frame(geometry, frame_index)
    if resolved is None:
        raise HTTPException(404, detail="mask is outside at this frame")
    return deepcopy(resolved["mask"])


def _validation_shapes(kind, geometry):
    value = deepcopy(geometry)
    if kind == "annotation":
        from pydantic import ValidationError
        from app.schemas._jsonb_types import VideoTrackMaskGeometry

        try:
            VideoTrackMaskGeometry.model_validate(value)
        except (ValidationError, ValueError, TypeError):
            _reject("prediction_video_mask_changed")
        # Persisted human keyframes are legitimate annotations. Only the copy
        # supplied to the shared prediction-budget validator changes; receipt
        # fingerprints and final subject comparisons retain the real geometry.
        for frame in value["keyframes"]:
            frame["source"] = "prediction"
    return [{"type": value["type"], "geometry": value}]


async def _subject(db, task_id, kind, identifier, shape_index, *, lock=False):
    from app.services.prediction import to_internal_shape

    if kind == "candidate":
        row, metadata = await accept._candidate(db, task_id, identifier, lock=lock)
        evidence = accept._qualified(row, metadata)
        if (
            type(shape_index) is not int
            or not 0 <= shape_index < len(row.result)
            or shape_index in (row.rejected_shape_indexes or [])
        ):
            raise HTTPException(404, detail="Prediction shape not found")
        geometry = to_internal_shape(deepcopy(row.result[shape_index])).get("geometry")
        return geometry, evidence, accept._request_fingerprint(row, metadata)
    row = await _annotation(db, task_id, identifier, lock=lock)
    lineage = _lineage(row)
    # This namespace can be inherited by server clone/slice operations on the
    # same source. Caller-supplied source/parent/attributes never creates it.
    return deepcopy(row.geometry), lineage, _annotation_fingerprint(row)


@dataclass(frozen=True, slots=True)
class _ReadReceipt:
    issuer: object = field(repr=False)
    session: object = field(repr=False)
    transaction: object = field(repr=False)
    actor_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    identifier: uuid.UUID
    shape_index: int | None
    frame_index: int
    credentials: str = field(repr=False)
    binding: str = field(repr=False)
    fingerprint: str
    geometry: str = field(repr=False)
    proof: str = field(repr=False)
    masks: str = field(repr=False)
    reference: str = field(repr=False)
    content: str = field(repr=False)


def _source_matches(kind, proof, binding):
    if kind == "candidate":
        expected = {key: proof[key] for key in references._TASK_FIELDS}
        valid = accept._same(
            accept._without_version(binding), accept._without_version(expected)
        )
    else:
        valid = proof["source_digest"] == common.digest(binding["original"]) and proof[
            "binding_digest"
        ] == _binding_digest(binding)
    if not valid:
        _reject("content_binding_changed")


@accept._busy
async def _qualify(
    db, task, actor, request, *, kind, identifier, shape_index, frame_index
):
    if db.sync_session.in_nested_transaction():
        _reject()
    task_id, project_id, actor_id = task.id, task.project_id, actor.id
    source = failure = None
    try:
        geometry, proof, fingerprint = await _subject(
            db, task_id, kind, identifier, shape_index
        )
        credentials = await common.capture_credentials(db, actor, request)
        task, binding = await _current(db, task_id, project_id, actor_id, credentials)
        _source_matches(kind, proof, binding)
        probe = originals.normalize_probe(proof["probe"])
        reference = _requested(geometry, probe, frame_index)
        await common.settled(db.commit())
        content = {}
        masks = await accept.qualify_mask_references(
            db,
            _validation_shapes(kind, geometry),
            probe,
            content=content,
            content_key=reference["object_key"],
        )
        if kind == "candidate":
            accept._generation_masks(proof, masks)

        async def guard(owned):
            await _current(
                owned, task_id, project_id, actor_id, credentials, binding=binding
            )

        source = await common.settled(
            originals.open_snapshot(
                db,
                task_id,
                binding["original"],
                guard=guard,
                purpose="video_mask_read_" + uuid.uuid4().hex,
                node_id=api_node_id(),
            ),
            late=lambda value: value.aclose(),
        )
        if not accept._same(originals.normalize_probe(source.probe), probe):
            _reject("prediction_video_probe_changed")
        await common.settled(source.aclose())
        if not source.closed or not source.settled or source.unsettled:
            raise storage_access.mark_unsettled(
                ContentError("prediction_video_source_unsettled", 503)
            )
        await _current(db, task_id, project_id, actor_id, credentials, binding=binding)
        current_geometry, current_proof, current_fingerprint = await _subject(
            db, task_id, kind, identifier, shape_index, lock=True
        )
        if (
            current_fingerprint != fingerprint
            or not accept._same(current_proof, proof)
            or not accept._same(current_geometry, geometry)
        ):
            _reject("prediction_video_mask_changed")
        await accept.lock_mask_references(
            db, _validation_shapes(kind, geometry), probe, masks
        )
        return _ReadReceipt(
            _ISSUER,
            db.sync_session,
            db.sync_session.get_transaction(),
            actor_id,
            task_id,
            project_id,
            kind,
            identifier,
            shape_index,
            frame_index,
            common.encoded(credentials),
            common.encoded(binding),
            fingerprint,
            common.encoded(geometry),
            common.encoded(proof),
            common.encoded(masks),
            common.encoded(reference),
            common.encoded(content[reference["object_key"]]),
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if source is not None and (not source.settled or source.unsettled):
            try:
                await common.settled(source.aclose(failure))
            except BaseException as closing:
                if failure is not None:
                    common._uncertain(failure, closing)
                else:
                    raise


async def qualify_candidate_read(
    db, task, actor, request, prediction_id, shape_index, frame_index
):
    return await _qualify(
        db,
        task,
        actor,
        request,
        kind="candidate",
        identifier=prediction_id,
        shape_index=shape_index,
        frame_index=frame_index,
    )


async def qualify_annotation_read(db, task, actor, request, annotation_id, frame_index):
    return await _qualify(
        db,
        task,
        actor,
        request,
        kind="annotation",
        identifier=annotation_id,
        shape_index=None,
        frame_index=frame_index,
    )


@accept._busy
async def validate_geometry_receipt(db, task, receipt, geometry):
    if (
        type(receipt) is not _ReadReceipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task.id
        or receipt.project_id != task.project_id
        or not accept._same(geometry, json.loads(receipt.geometry))
    ):
        _reject()
    await _current(
        db,
        receipt.task_id,
        receipt.project_id,
        receipt.actor_id,
        json.loads(receipt.credentials),
        binding=json.loads(receipt.binding),
    )
    current, proof, fingerprint = await _subject(
        db, task.id, receipt.kind, receipt.identifier, receipt.shape_index, lock=True
    )
    if (
        fingerprint != receipt.fingerprint
        or not accept._same(current, geometry)
        or not accept._same(proof, json.loads(receipt.proof))
    ):
        _reject("prediction_video_mask_changed")
    await accept.lock_mask_references(
        db,
        _validation_shapes(receipt.kind, geometry),
        proof["probe"],
        json.loads(receipt.masks),
    )


async def read_content(db, task, receipt, reference):
    """Return only the pre-read requested RLE; there is no provider I/O here."""
    if type(receipt) is not _ReadReceipt or receipt.issuer is not _ISSUER:
        _reject()
    await validate_geometry_receipt(db, task, receipt, json.loads(receipt.geometry))
    if not accept._same(reference, json.loads(receipt.reference)):
        _reject()
    return json.loads(receipt.content)
