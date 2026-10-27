"""Current-caller qualification of persisted canonical whole-video candidates.

Generation credentials never become adoption authority. A complete original
read and probe, including actual source close, precedes final business locks.
The resulting receipt grants only its exact candidate and reviewed selection.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from functools import wraps
import json
import math
import uuid

from fastapi import HTTPException
from sqlalchemy import Text, case, cast, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import attributes, defer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.db.models.raster_mask_object import RasterMaskObject
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.services import prediction_originals as common
from app.services import storage_access, video_prediction_originals as originals
from app.services import video_prediction_references as references
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.labeling_core_read_relations import (
    annotation_row,
    lock_task_for_core_read,
    prediction_meta_row,
    prediction_row,
)
from app.services.project_access import get_workspace_project
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.task_scope import lock_task_scope

PROOF_KEY = "video_prediction_proof"
MAX_JSON_BYTES = 64 * 1024**2
MAX_SHAPES = 100_000
_ISSUER = object()
_EVIDENCE_FIELDS = references._TASK_FIELDS | {
    "schema",
    "media_type",
    "job_id",
    "actor_id",
    "node_id",
    "execution_snapshot_id",
    "execution_digest",
    "probe",
    "models",
    "input_id",
    "input_version_id",
}


def _reject(code="prediction_video_candidate_invalid", status=409):
    raise ContentError(code, status)


def _json(value, *, limit=MAX_JSON_BYTES):
    try:
        raw = common.encoded(value).encode()
        if len(raw) > limit:
            _reject("prediction_video_candidate_budget_exceeded", 413)
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        if isinstance(exc, ContentError):
            raise
        _reject()


def _same(left, right):
    return common.encoded(left) == common.encoded(right)


def _sha(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        _reject()


def _evidence(value):
    value = _json(value)
    if (
        not isinstance(value, dict)
        or set(value) - {"result_qualification"} != _EVIDENCE_FIELDS
    ):
        _reject()
    if (
        type(value["schema"]) is not int
        or value["schema"] != 1
        or value["media_type"] != "video"
    ):
        _reject()
    binding = references._binding({key: value[key] for key in references._TASK_FIELDS})
    relation = binding["original"]["facts"]["relation"]
    if any(
        relation[key] != binding[target]
        for key, target in (
            ("task_id", "task_id"),
            ("batch_id", "batch_id"),
            ("direct_item_id", "item_id"),
            ("file_path", "file_path"),
        )
    ):
        _reject()
    for name in ("job_id", "actor_id", "execution_snapshot_id", "input_id"):
        references._uuid(value[name])
    _sha(value["execution_digest"])
    if (
        not isinstance(value["node_id"], str)
        or not value["node_id"]
        or len(value["node_id"]) > 128
    ):
        _reject()
    if (
        not isinstance(value["input_version_id"], str)
        or not 0 < len(value["input_version_id"]) <= 1024
        or value["input_version_id"] == "null"
    ):
        _reject()
    originals.normalize_probe(value["probe"])
    rows = value["models"]
    if not isinstance(rows, list) or not rows:
        _reject()
    declarations = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != references._MODEL_BINDING_FIELDS | {
            "executed_registry_id"
        }:
            _reject()
        actual = row["executed_registry_id"]
        if actual is not None:
            references._uuid(actual)
        if row["parent_stage"] is None and actual is None:
            _reject()
        declarations.append({key: row[key] for key in references._MODEL_BINDING_FIELDS})
    references._models(declarations)
    if "result_qualification" in value:
        qualification = value["result_qualification"]
        if (
            not isinstance(qualification, dict)
            or type(qualification.get("schema")) is not int
            or qualification["schema"] != 1
            or not isinstance(qualification.get("stages"), list)
        ):
            _reject()
    return value


def _metadata(extra):
    if not isinstance(extra, dict):
        _reject()
    # Retry may append its superseding candidate after publication. That field
    # is checked as current state, while all original metadata stays frozen.
    return {
        key: value
        for key, value in extra.items()
        if key not in {PROOF_KEY, "superseded_by_prediction_id"}
    }


def _candidate_value(prediction, extra):
    if not isinstance(prediction.result, list) or len(prediction.result) > MAX_SHAPES:
        _reject("prediction_video_candidate_budget_exceeded", 413)
    created = prediction.created_at
    if created is None or created.utcoffset() is None:
        _reject()
    return {
        "id": str(prediction.id),
        "created_at": created.isoformat(),
        "task_id": str(prediction.task_id),
        "project_id": str(prediction.project_id),
        "source": prediction.source,
        "tool_unit_id": prediction.tool_unit_id,
        "model_version": prediction.model_version,
        "score": prediction.score,
        "result": prediction.result,
        "metadata": _metadata(extra),
    }


def persisted_qualification(prediction, extra, evidence):
    """Seal actual stored result/metadata; never take this proof from a backend."""
    evidence = _evidence(evidence)
    if (
        prediction.source != "ml_backend"
        or evidence["task_id"] != str(prediction.task_id)
        or evidence["original"]["facts"]["relation"]["project_id"]
        != str(prediction.project_id)
    ):
        _reject()
    if (
        not isinstance(extra, dict)
        or PROOF_KEY in extra
        or not _same(
            extra.get("original_video_input"), references.public_provenance(evidence)
        )
    ):
        _reject()
    candidate = _json(_candidate_value(prediction, extra))
    return _json(
        {
            "schema": 1,
            "kind": "video_prediction",
            "evidence": evidence,
            "candidate_digest": common.digest(
                {"candidate": candidate, "evidence": evidence}
            ),
        },
        limit=MAX_JSON_BYTES,
    )


def _qualified(prediction, metadata):
    extra = metadata.extra if metadata is not None else None
    if not isinstance(extra, dict) or not isinstance(extra.get(PROOF_KEY), dict):
        _reject("prediction_video_requires_new_request")
    proof = _json(extra[PROOF_KEY])
    if (
        set(proof) != {"schema", "kind", "evidence", "candidate_digest"}
        or type(proof["schema"]) is not int
        or proof["schema"] != 1
        or proof["kind"] != "video_prediction"
    ):
        _reject()
    evidence = _evidence(proof["evidence"])
    if (
        prediction.source != "ml_backend"
        or evidence["task_id"] != str(prediction.task_id)
        or evidence["original"]["facts"]["relation"]["project_id"]
        != str(prediction.project_id)
        or not _same(
            extra.get("original_video_input"), references.public_provenance(evidence)
        )
        or proof["candidate_digest"]
        != common.digest(
            {
                "candidate": _json(_candidate_value(prediction, extra)),
                "evidence": evidence,
            }
        )
    ):
        _reject()
    if extra.get("superseded_by_prediction_id") is not None:
        _reject("prediction_video_candidate_superseded")
    return evidence


def requires_qualification(prediction, metadata=None):
    """Retain only the existing strict single-frame and external-import paths."""
    from app.services.prediction import to_internal_shape

    extra = metadata.extra if metadata is not None else None
    if isinstance(extra, dict) and (
        PROOF_KEY in extra or "original_video_input" in extra
    ):
        return True
    if prediction.source != "ml_backend":
        return False
    raw = prediction.result
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SHAPES:
        return True
    for shape in raw:
        if not isinstance(shape, dict):
            return True
        try:
            geometry = to_internal_shape(deepcopy(shape)).get("geometry")
        except (ValueError, TypeError, KeyError, OverflowError):
            return True
        if (
            not isinstance(geometry, dict)
            or set(geometry) != {"type", "frame_index", "x", "y", "w", "h"}
            or geometry["type"] != "video_bbox"
            or type(geometry["frame_index"]) is not int
            or geometry["frame_index"] < 0
        ):
            return True
        if any(
            type(geometry[key]) not in (int, float)
            or not math.isfinite(geometry[key])
            or not 0 <= geometry[key] <= 1
            for key in ("x", "y", "w", "h")
        ):
            return True
    return False


def selection_value(
    shape_index=None,
    override_class_name=None,
    attribute_overrides=None,
    video_segment_id=None,
):
    if shape_index is not None and (type(shape_index) is not int or shape_index < 0):
        raise ValueError("invalid shape index")
    if override_class_name is not None and not isinstance(override_class_name, str):
        raise ValueError("invalid override class")
    if attribute_overrides is not None and not isinstance(attribute_overrides, dict):
        raise ValueError("invalid attribute overrides")
    return _json(
        {
            "shape_index": shape_index,
            "override_class_name": override_class_name,
            "attribute_overrides": attribute_overrides,
            "video_segment_id": str(uuid.UUID(str(video_segment_id)))
            if video_segment_id is not None
            else None,
        },
        limit=references.MAX_PROOF_BYTES,
    )


async def _request_selection(request):
    query = request.query_params
    index = query.get("shape_index")
    try:
        index = int(index) if index is not None else None
        raw = await request.body()
        body = await request.json() if raw else None
        if body is not None and not isinstance(body, dict):
            raise ValueError
        return selection_value(
            index,
            query.get("override_class_name"),
            body.get("attribute_overrides") if body else None,
            query.get("video_segment_id"),
        )
    except (ValueError, TypeError):
        raise HTTPException(
            422, detail="Invalid prediction acceptance selection"
        ) from None


async def _bounded_row(db, model, identifier, json_fields, *, lock=False):
    columns = [getattr(model, name) for name in json_fields]
    size = sum(
        func.coalesce(func.octet_length(cast(column, Text)), 0) for column in columns
    )
    allowed = size <= MAX_JSON_BYTES
    query = (
        select(
            model, allowed, *(case((allowed, column), else_=None) for column in columns)
        )
        .options(*(defer(column) for column in columns))
        .where(model.id == identifier)
    )
    if lock:
        from app.services.labeling_core_read_relations import (
            lock_prediction_for_core_read_share,
            lock_prediction_meta_for_core_read_share,
        )

        if model is prediction_row:
            await lock_prediction_for_core_read_share(db, identifier)
        elif model is prediction_meta_row:
            await lock_prediction_meta_for_core_read_share(db, identifier)
    row = (
        await db.execute(query.execution_options(populate_existing=True))
    ).one_or_none()
    if row is None:
        return None
    value, within_budget, *json_values = row
    if not within_budget:
        _reject("prediction_video_candidate_budget_exceeded", 413)
    for name, member in zip(json_fields, json_values, strict=True):
        attributes.set_committed_value(value, name, member)
    return value


def _busy(call):
    @wraps(call)
    async def invoke(*args, **kwargs):
        try:
            return await call(*args, **kwargs)
        except DBAPIError as exc:
            if (
                storage_access.unavailable(exc)
                or getattr(exc.orig, "sqlstate", None) == "40001"
            ):
                raise ContentError("prediction_video_acceptance_busy") from exc
            raise

    return invoke


async def _candidate(db, task_id, prediction_id, *, lock=False):
    prediction = await _bounded_row(
        db,
        prediction_row,
        prediction_id,
        ("result", "rejected_shape_indexes"),
        lock=lock,
    )
    if prediction is None or prediction.task_id != task_id:
        raise HTTPException(404, detail="Prediction not found")
    metadata_id = await db.scalar(
        select(prediction_meta_row.id).where(
            prediction_meta_row.prediction_id == prediction.id
        )
    )
    metadata = (
        await _bounded_row(db, prediction_meta_row, metadata_id, ("extra",), lock=lock)
        if metadata_id
        else None
    )
    return prediction, metadata


async def _authorize(db, task, actor):
    from app.services import task_access
    from app.services.project_write_guard import assert_phase_write_allowed

    project = await get_workspace_project(
        db,
        actor_id=actor.id if actor is not None else None,
        project_id=task.project_id,
        populate_existing=True,
    )
    if (
        actor is None
        or not actor.is_active
        or project is None
        or project.data_type != "video"
        or task.file_type != "video"
    ):
        _reject("prediction_acceptance_authority_revoked", 403)
    access = await task_access.resolve_task_access(
        db, task, actor.id, lock_membership=False
    )
    task_access.assert_annotation_write_allowed(task, access)
    await task_access.assert_task_visible(db, task, actor.id, access=access)
    task_access.assert_task_editable(task, actor.id, access=access)
    await assert_phase_write_allowed(
        db, task, actor, action="accept prediction", lock_membership=False
    )
    if any(
        row.user_id != actor.id
        for row in await TaskLockService(db).active_locks(task.id, cleanup=False)
    ):
        _reject("prediction_acceptance_task_locked")


async def _credentials(db, value, actor_id):
    common.check_clock(value)
    key_id = uuid.UUID(value["api_key_id"]) if value["api_key_id"] else None
    actor = await common.reauthorize(db, value, actor_id, key_id)
    if key_id is not None:
        await key_authority(db, actor, key_id, "annotations:write")
    return actor


def _without_version(binding):
    return {key: value for key, value in binding.items() if key != "version"}


async def _current(
    db, task_id, project_id, actor_id, credentials, *, binding=None, final=False
):
    await lock_actor_project_authority(db, actor_id, project_id)
    await lock_actor_scope(db, actor_id, project_id)
    actor = await _credentials(db, credentials, actor_id)
    if final:
        try:
            await TaskLockService(db)._lock_task_scope(task_id, nowait=True)
        except TaskLockConflictError:
            _reject("prediction_video_acceptance_busy")
        await lock_task_scope(db, [task_id])
    task = await common.locked_task(db, task_id)
    if final:
        task = await lock_task_for_core_read(
            db,
            task_id,
            project_id=project_id,
            lock_mode="update",
            nowait=True,
            populate_existing=True,
        )
    if (
        task is None
        or task.project_id != project_id
        or task.file_type != "video"
        or is_reference(task.file_path)
    ):
        _reject("content_binding_changed")
    await _authorize(db, task, actor)
    current = references._binding(
        {
            **task_binding(task),
            "original": await originals.managed_binding(db, task, lock=True),
        }
    )
    if binding is not None and not _same(current, binding):
        _reject("content_binding_changed")
    return task, actor, current


async def _selected(db, prediction, selection):
    raw = prediction.result
    if not isinstance(raw, list) or len(raw) > MAX_SHAPES:
        _reject()
    rejected = prediction.rejected_shape_indexes
    if not isinstance(rejected, list) or any(
        type(index) is not int or not 0 <= index < len(raw) for index in rejected
    ):
        _reject()
    index = selection["shape_index"]
    if index is not None and index >= len(raw):
        raise HTTPException(
            404, detail="Prediction not found or shape_index out of range"
        )
    consumed = (
        (
            await db.execute(
                select(annotation_row.attributes["_shape_index"].astext)
                .where(annotation_row.parent_prediction_id == prediction.id)
                .limit(MAX_SHAPES + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(consumed) > MAX_SHAPES:
        _reject("prediction_video_candidate_budget_exceeded", 413)
    adopted = set()
    for value in consumed:
        if (
            not isinstance(value, str)
            or not value.isdecimal()
            or str(int(value)) != value
            or int(value) >= len(raw)
        ):
            _reject("prediction_video_candidate_already_accepted")
        adopted.add(int(value))
    if index is not None:
        if index in rejected:
            _reject("prediction_video_candidate_rejected")
        if index in adopted:
            _reject("prediction_video_candidate_already_accepted")
        return [index]
    result = sorted(set(range(len(raw))) - set(rejected) - adopted)
    if not result:
        _reject("prediction_video_candidate_already_accepted")
    return result


async def _segment(db, task, actor, selection, prediction, indexes, *, lock=False):
    from app.services.prediction import to_internal_shape
    from app.services.video_collaboration import assert_video_annotation_write_scope

    identifier = (
        uuid.UUID(selection["video_segment_id"])
        if selection["video_segment_id"]
        else None
    )
    if lock and identifier is not None:
        # The Item also fences new Segment FKs that would change work bounds.
        if task.dataset_item_id is not None:
            await db.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id == task.dataset_item_id)
                .with_for_update(nowait=True)
            )
        from app.services.labeling_core_read_relations import (
            lock_video_segment_for_core_read_share,
        )

        await lock_video_segment_for_core_read_share(
            db, identifier, task.dataset_item_id
        )
    await assert_video_annotation_write_scope(
        db, task=task, user=actor, segment_id=identifier, geometry=None
    )
    for index in indexes:
        geometry = to_internal_shape(deepcopy(prediction.result[index])).get("geometry")
        await assert_video_annotation_write_scope(
            db, task=task, user=actor, segment_id=identifier, geometry=geometry
        )


def _request_fingerprint(prediction, metadata):
    return common.digest(
        {
            "candidate": _candidate_value(prediction, metadata.extra),
            "rejected": prediction.rejected_shape_indexes,
        }
    )


def _saved_results(value, probe):
    from app.services.video_prediction_results import validate_saved_results

    validate_saved_results(value, probe)


def _references(value):
    from app.services.raster_mask_storage import collect_mask_references

    result = {}
    for reference in collect_mask_references(value):
        key = reference.get("object_key")
        if key in result and not _same(result[key], reference):
            _reject("prediction_video_mask_changed")
        result[key] = deepcopy(reference)
    return {key: result[key] for key in sorted(result)}


def _mask_owner_value(receipt):
    from app.services import raster_object_storage as physical

    return _json(
        {
            **{name: getattr(receipt, name) for name in physical._FACTS},
            "storage_ref": receipt.storage_ref,
        }
    )


@asynccontextmanager
async def _mask_session(db):
    session = AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    )
    try:
        yield session
    finally:
        await common.settled(session.close())


async def _mask_owner(db, reference, *, lock=False):
    from app.services import raster_mask_objects, raster_object_storage as physical

    query = select(RasterMaskObject).where(
        RasterMaskObject.logical_key == reference["object_key"],
        RasterMaskObject.state == "ready",
    )
    if lock:
        query = query.with_for_update(read=True, nowait=True)
    row = await db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        _reject("prediction_video_mask_changed")
    receipt = physical.snapshot(row)
    raster_mask_objects._matching_reference(reference, receipt)
    location_id = uuid.UUID(receipt.storage_ref["location_id"])
    if lock:
        gate = (
            (
                await db.execute(
                    select(
                        StorageMaintenanceGate.state, StorageMaintenanceGate.revision
                    )
                    .where(StorageMaintenanceGate.location_id == location_id)
                    .with_for_update(read=True, nowait=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if gate is None:
            _reject("prediction_video_mask_changed")
        revision = storage_access.validate(gate, None)
    else:
        revision = await storage_access.check(db, location_id)
    return {
        "reference": deepcopy(reference),
        "receipt": _mask_owner_value(receipt),
        "gate_revision": revision,
    }


@_busy
async def qualify_mask_references(db, value, probe, *, content=None, content_key=None):
    """Read all actual RLE content before business locks; freeze saved owners."""
    from app.services import raster_mask_objects
    from app.services.raster_mask_storage import validate_reference_for_rle

    if db.in_transaction():
        _reject("prediction_video_io_transaction_open")
    _saved_results(value, probe)
    references_to_read = _references(value)
    if content is not None and content_key not in references_to_read:
        _reject("prediction_video_mask_changed")
    facts = []
    if not references_to_read:
        return facts
    async with _mask_session(db) as owned:
        for reference in references_to_read.values():
            before = await _mask_owner(owned, reference)
            await common.settled(owned.rollback())
            rle = await common.settled(raster_mask_objects.load(reference, db=owned))
            validate_reference_for_rle(reference, rle)
            if rle["size"] != [probe["video"]["height"], probe["video"]["width"]]:
                _reject("prediction_video_mask_changed")
            after = await _mask_owner(owned, reference)
            await common.settled(owned.rollback())
            if not _same(before, after):
                _reject("prediction_video_mask_changed")
            if content is not None and reference["object_key"] == content_key:
                content[content_key] = deepcopy(rle)
            facts.append(before)
    _saved_results(value, probe)
    return _json(facts)


def _mask_facts(facts):
    facts = _json(facts)
    if not isinstance(facts, list):
        _reject("prediction_video_mask_changed")
    result = {}
    for entry in facts:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"reference", "receipt", "gate_revision"}
            or not isinstance(entry["reference"], dict)
            or not isinstance(entry["receipt"], dict)
            or type(entry["gate_revision"]) is not int
            or entry["gate_revision"] < 1
        ):
            _reject("prediction_video_mask_changed")
        key = entry["reference"].get("object_key")
        if not isinstance(key, str) or key in result:
            _reject("prediction_video_mask_changed")
        result[key] = entry
    return result


@_busy
async def lock_mask_references(db, value, probe, facts):
    """Only SQL: consumers must first validate their private source receipt."""
    from app.services.raster_mask_storage import lock_raster_mask_references

    _saved_results(value, probe)
    expected = _mask_facts(facts)
    current_refs = _references(value)
    if set(current_refs) != set(expected) or any(
        not _same(reference, expected[key]["reference"])
        for key, reference in current_refs.items()
    ):
        _reject("prediction_video_mask_changed")
    await lock_raster_mask_references(db, value, verify=False)
    for key, reference in current_refs.items():
        current = await _mask_owner(db, reference, lock=True)
        if not _same(current, expected[key]):
            _reject("prediction_video_mask_changed")


def _generation_masks(evidence, facts):
    if not facts:
        return
    saved = (evidence.get("result_qualification") or {}).get("masks")
    if saved is None:
        _reject("prediction_video_requires_new_request")
    original = _mask_facts(saved)
    for key, fact in _mask_facts(facts).items():
        if key not in original or not _same(fact, original[key]):
            _reject("prediction_video_mask_changed")


def _selected_value(prediction, indexes):
    from app.services.prediction import to_internal_shape

    return [to_internal_shape(deepcopy(prediction.result[index])) for index in indexes]


@dataclass(frozen=True, slots=True)
class _AcceptanceReceipt:
    issuer: object = field(repr=False)
    session: object = field(repr=False)
    transaction: object = field(repr=False)
    actor_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    prediction_id: uuid.UUID
    credentials: str = field(repr=False)
    binding: str = field(repr=False)
    evidence: str = field(repr=False)
    candidate: str = field(repr=False)
    selection: str = field(repr=False)
    selected: tuple[int, ...]
    masks: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _RejectionReceipt:
    issuer: object = field(repr=False)
    session: object = field(repr=False)
    transaction: object = field(repr=False)
    actor_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    prediction_id: uuid.UUID
    credentials: str = field(repr=False)


def selected_shape_indexes(receipt):
    if type(receipt) is not _AcceptanceReceipt or receipt.issuer is not _ISSUER:
        _reject("prediction_video_acceptance_receipt_invalid")
    return list(receipt.selected)


async def qualify_acceptance(db, task, actor, request, prediction_id):
    if db.sync_session.in_nested_transaction():
        _reject("prediction_video_acceptance_receipt_invalid")
    task_id, project_id, actor_id = task.id, task.project_id, actor.id
    selection = await _request_selection(request)
    source = failure = None
    try:
        prediction, metadata = await _candidate(db, task_id, prediction_id)
        evidence = _qualified(prediction, metadata)
        credentials = await common.capture_credentials(db, actor, request)
        task, actor, binding = await _current(
            db, task_id, project_id, actor_id, credentials
        )
        expected = {key: evidence[key] for key in references._TASK_FIELDS}
        if not _same(_without_version(binding), _without_version(expected)):
            _reject("content_binding_changed")
        indexes = await _selected(db, prediction, selection)
        await _segment(db, task, actor, selection, prediction, indexes)
        fingerprint = _request_fingerprint(prediction, metadata)
        await common.settled(db.commit())
        selected_value = _selected_value(prediction, indexes)
        mask_facts = await qualify_mask_references(
            db, selected_value, evidence["probe"]
        )
        _generation_masks(evidence, mask_facts)

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
                purpose="video_accept_" + uuid.uuid4().hex,
                node_id=api_node_id(),
            ),
            late=lambda value: value.aclose(),
        )
        if not _same(originals.normalize_probe(source.probe), evidence["probe"]):
            _reject("prediction_video_probe_changed")
        await common.settled(source.aclose())
        if not source.closed or not source.settled or source.unsettled:
            raise storage_access.mark_unsettled(
                ContentError("prediction_video_source_unsettled", 503)
            )
        # No source or independent-session handle survives into these locks.
        task, actor, current = await _current(
            db, task_id, project_id, actor_id, credentials, binding=binding, final=True
        )
        prediction, metadata = await _candidate(db, task_id, prediction_id, lock=True)
        final_evidence = _qualified(prediction, metadata)
        if (
            not _same(final_evidence, evidence)
            or _request_fingerprint(prediction, metadata) != fingerprint
        ):
            _reject()
        final_indexes = await _selected(db, prediction, selection)
        if final_indexes != indexes:
            _reject("prediction_video_candidate_changed")
        await _segment(db, task, actor, selection, prediction, final_indexes, lock=True)
        await lock_mask_references(
            db,
            _selected_value(prediction, final_indexes),
            evidence["probe"],
            mask_facts,
        )
        return _AcceptanceReceipt(
            _ISSUER,
            db.sync_session,
            db.sync_session.get_transaction(),
            actor_id,
            task_id,
            project_id,
            prediction_id,
            common.encoded(credentials),
            common.encoded(current),
            common.encoded(evidence),
            fingerprint,
            common.encoded(selection),
            tuple(final_indexes),
            common.encoded(mask_facts),
        )
    except BaseException as exc:
        failure = exc
        if isinstance(exc, DBAPIError) and (
            storage_access.unavailable(exc)
            or getattr(exc.orig, "sqlstate", None) == "40001"
        ):
            failure = ContentError("prediction_video_acceptance_busy")
            raise failure from exc
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


def _receipt(db, receipt, *, actor_id=None, prediction=None):
    if (
        type(receipt) is not _AcceptanceReceipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or actor_id is not None
        and receipt.actor_id != actor_id
        or prediction is not None
        and (
            receipt.task_id != prediction.task_id
            or receipt.prediction_id != prediction.id
            or receipt.project_id != prediction.project_id
        )
    ):
        _reject("prediction_video_acceptance_receipt_invalid")
    return receipt


@_busy
async def validate_acceptance(db, prediction, actor_id, receipt, *, selection=None):
    receipt = _receipt(db, receipt, actor_id=actor_id, prediction=prediction)
    if selection is None or not _same(selection, json.loads(receipt.selection)):
        _reject("prediction_video_acceptance_receipt_invalid")
    credentials = json.loads(receipt.credentials)
    task, actor, _ = await _current(
        db,
        receipt.task_id,
        receipt.project_id,
        actor_id,
        credentials,
        binding=json.loads(receipt.binding),
        final=True,
    )
    current, metadata = await _candidate(db, task.id, prediction.id, lock=True)
    evidence = _qualified(current, metadata)
    if (
        not _same(evidence, json.loads(receipt.evidence))
        or _request_fingerprint(current, metadata) != receipt.candidate
    ):
        _reject("prediction_video_acceptance_receipt_invalid")
    indexes = await _selected(db, current, selection)
    if indexes != list(receipt.selected):
        _reject("prediction_video_acceptance_receipt_invalid")
    await _segment(db, task, actor, selection, current, indexes, lock=True)
    await lock_mask_references(
        db,
        _selected_value(current, indexes),
        evidence["probe"],
        json.loads(receipt.masks),
    )
    return references.public_provenance(evidence)


@_busy
async def validate_geometry_receipt(db, task, receipt, geometry):
    """Qualify only a geometry in this selected candidate; never perform I/O."""
    receipt = _receipt(db, receipt)
    if receipt.task_id != task.id or receipt.project_id != task.project_id:
        _reject("prediction_video_acceptance_receipt_invalid")
    prediction, _ = await _candidate(db, task.id, receipt.prediction_id, lock=True)
    selected = _selected_value(prediction, receipt.selected)
    if not any(_same(shape.get("geometry"), geometry) for shape in selected):
        _reject("prediction_video_acceptance_receipt_invalid")
    return await validate_acceptance(
        db,
        prediction,
        receipt.actor_id,
        receipt,
        selection=json.loads(receipt.selection),
    )


@_busy
async def recheck_credentials(db, receipt):
    """Use after own writes; do not classify these newly inserted rows as replay."""
    if type(receipt) is _RejectionReceipt:
        if (
            receipt.issuer is not _ISSUER
            or receipt.session is not db.sync_session
            or receipt.transaction is None
            or receipt.transaction is not db.sync_session.get_transaction()
            or db.sync_session.in_nested_transaction()
        ):
            _reject("prediction_video_acceptance_receipt_invalid")
    else:
        receipt = _receipt(db, receipt)
    await _credentials(db, json.loads(receipt.credentials), receipt.actor_id)


@_busy
async def qualify_rejection(db, task, actor, request, prediction_id):
    """Current write authority only: even an offline original can be rejected."""
    if db.sync_session.in_nested_transaction():
        _reject("prediction_video_acceptance_receipt_invalid")
    task_id, project_id, actor_id = task.id, task.project_id, actor.id
    credentials = await common.capture_credentials(db, actor, request)
    await lock_actor_project_authority(db, actor_id, project_id)
    await lock_actor_scope(db, actor_id, project_id)
    actor = await _credentials(db, credentials, actor_id)
    try:
        await TaskLockService(db)._lock_task_scope(task_id, nowait=True)
    except TaskLockConflictError:
        _reject("prediction_video_acceptance_busy")
    await lock_task_scope(db, [task_id])
    task = await lock_task_for_core_read(
        db,
        task_id,
        project_id=project_id,
        lock_mode="update",
        nowait=True,
        populate_existing=True,
    )
    if task is None or task.project_id != project_id or task.file_type != "video":
        _reject("content_binding_changed")
    await _authorize(db, task, actor)
    await _candidate(db, task_id, prediction_id, lock=True)
    return _RejectionReceipt(
        _ISSUER,
        db.sync_session,
        db.sync_session.get_transaction(),
        actor_id,
        task_id,
        project_id,
        prediction_id,
        common.encoded(credentials),
    )
