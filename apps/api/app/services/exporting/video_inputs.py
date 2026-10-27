"""Bounded metadata-only video inputs, before probing or reading any original."""

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
import uuid

from sqlalchemy import Text, and_, bindparam, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene, VideoSegment
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.video_track_quality import VideoTrackQualityRun
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.exporting import image_inputs, lidar_inputs
from app.services.exporting.video_media_paths import TARGETS, logical_sources
from app.services.exporting.video_frozen import FrozenVideo, scope_from_dict
from app.services.raster_mask_storage import collect_mask_references
from app.services.reference_keys import is_reference
from aap_workspace_contract import derive_classes_list
from app.services.task_scope import ids_clause

CONTRACT = 4
PROBE_CONTRACT = 1
MAX_TASKS = 1000
MAX_LINKS = 50_000
MAX_ANNOTATIONS = MAX_PREDICTIONS = MAX_SEGMENTS = MAX_QUALITY_RUNS = 100_000
MAX_QUALITY_PAIRS = 100_000
MAX_QUALITY_SAMPLE_FRAMES = 1_000_000
MAX_OBJECT_BYTES = 5 * 1024**3
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_JSON_BYTES = 256 * 1024**2
MAX_TRACK_KEYFRAMES = MAX_TRACK_OUTSIDE = 10_000
MAX_KEYFRAME_VERTICES = 1024
MAX_TOTAL_KEYFRAMES = MAX_TOTAL_OUTSIDE = 1_000_000
MAX_TOTAL_VERTICES = MAX_MASK_RUNS = 4_000_000
_PROJECT_FIELDS = (
    *image_inputs._PROJECT_FIELDS,
    "video_sampling",
    "video_collaboration",
)
_QUALITY_FIELDS = (
    "id",
    "task_id",
    "left_segment_id",
    "right_segment_id",
    "created_at",
    "status",
    "input_digest",
    "sampling_digest",
    "pairs",
)


def reject(code="video_export_inputs_changed", status=409):
    raise ContentError(code, status)


def _translate(exc):
    for prefix in ("lidar_export_", "image_export_"):
        if exc.code.startswith(prefix):
            reject(exc.code.replace(prefix, "video_export_", 1), exc.status)
    raise exc


def digest(data):
    try:
        return lidar_inputs.digest(data, limit=MAX_JSON_BYTES)
    except ContentError as exc:
        _translate(exc)


def uses_contract(project, targets):
    return (
        project.data_type == "video"
        and bool(targets)
        and all(isinstance(target, str) and target in TARGETS for target in targets)
    )


def _selection(task_ids):
    if task_ids is None:
        return None
    if (
        not isinstance(task_ids, (list, tuple))
        or not task_ids
        or len(task_ids) > MAX_TASKS
    ):
        reject("video_export_scope_changed")
    try:
        selected = sorted(
            {uuid.UUID(str(identifier)) for identifier in task_ids}, key=str
        )
    except (ValueError, TypeError, AttributeError):
        reject("video_export_scope_changed")
    if len(selected) != len(task_ids):
        reject("video_export_scope_changed")
    return selected


@dataclass(frozen=True)
class Inputs:
    _snapshot: dict
    sources: object

    def __post_init__(self):
        object.__setattr__(self, "_snapshot", deepcopy(self._snapshot))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    @property
    def snapshot(self):
        return deepcopy(self._snapshot)

    @property
    def digest(self):
        return digest(self._snapshot)


class _Budget(lidar_inputs._Budget):
    def extra(self, data):
        try:
            self.media_bytes += lidar_inputs.encoded_size(data)
        except ContentError as exc:
            _translate(exc)
        if self.media_bytes + self.read_bytes > MAX_JSON_BYTES:
            reject("video_export_input_budget_exceeded", 422)


async def _rows(
    db,
    model,
    predicate,
    *,
    budget,
    max_rows=MAX_LINKS,
    lock=False,
    write=False,
    order=None,
):
    try:
        result = await lidar_inputs._rows(
            db,
            model,
            predicate,
            budget=budget,
            max_rows=max_rows,
            lock=lock,
            write=write,
            order=order,
        )
    except ContentError as exc:
        _translate(exc)
    if budget.read_bytes + budget.media_bytes > MAX_JSON_BYTES:
        reject("video_export_input_budget_exceeded", 422)
    return result


def geometry_budget(annotations, predictions=()):
    """Bound structure before clipping/interpolation; read no mask objects here."""
    total_keys = total_outside = total_vertices = 0

    def measure(geometry):
        nonlocal total_keys, total_outside, total_vertices
        if not isinstance(geometry, dict):
            return
        keys, outside = geometry.get("keyframes", []), geometry.get("outside", [])
        if not isinstance(keys, list) or not isinstance(outside, list):
            reject("video_export_geometry_invalid", 422)
        if len(keys) > MAX_TRACK_KEYFRAMES or len(outside) > MAX_TRACK_OUTSIDE:
            reject("video_export_input_budget_exceeded", 422)
        total_keys += len(keys)
        total_outside += len(outside)
        for entry in [geometry, *keys]:
            if not isinstance(entry, dict):
                reject("video_export_geometry_invalid", 422)
            points = entry.get("points", [])
            if not isinstance(points, list):
                reject("video_export_geometry_invalid", 422)
            if len(points) > MAX_KEYFRAME_VERTICES:
                reject("video_export_input_budget_exceeded", 422)
            total_vertices += len(points)
        if (
            total_keys > MAX_TOTAL_KEYFRAMES
            or total_outside > MAX_TOTAL_OUTSIDE
            or total_vertices > MAX_TOTAL_VERTICES
        ):
            reject("video_export_input_budget_exceeded", 422)

    for annotation in annotations:
        measure(annotation.geometry)
    for prediction in predictions:
        for reference in prediction.result or []:
            if isinstance(reference, dict):
                # LS shapes retain geometry under value; recursive mask facts
                # are budgeted separately without interpreting provider output.
                measure(reference.get("geometry", {}))
    return {
        "keyframes": total_keys,
        "outside": total_outside,
        "vertices": total_vertices,
    }


def _mask_budget(annotations, predictions):
    references, total, runs = {}, 0, 0
    for value in [
        *(row.geometry for row in annotations),
        *(row.result for row in predictions),
    ]:
        for reference in collect_mask_references(value):
            key = reference.get("object_key")
            if not isinstance(key, str) or (
                key in references and references[key] != reference
            ):
                reject("video_export_mask_invalid", 422)
            references[key] = reference
    for reference in references.values():
        size, declared_runs = reference.get("bytes"), reference.get("runs")
        dimensions = reference.get("size")
        if (
            type(size) is not int
            or not 0 < size <= 4 * 1024**2
            or type(declared_runs) is not int
            or declared_runs < 1
            or not isinstance(dimensions, list)
            or len(dimensions) != 2
            or any(
                type(value) is not int or not 0 < value <= 4096 for value in dimensions
            )
            or dimensions[0] * dimensions[1] > 16_777_216
        ):
            reject("video_export_mask_invalid", 422)
        total += size
        runs += declared_runs
        if total > MAX_JSON_BYTES or runs > MAX_MASK_RUNS:
            reject("video_export_input_budget_exceeded", 422)
    return total


async def _quality_rows(db, predicate, *, budget, lock=False):
    """Guard only consumed Run facts, retaining the same SQL snapshot at hydration."""
    if lock:
        identities = list(
            await db.scalars(
                select(VideoTrackQualityRun.id)
                .where(predicate)
                .order_by(VideoTrackQualityRun.id)
                .limit(MAX_QUALITY_RUNS + 1)
                .with_for_update(read=True, nowait=True)
            )
        )
        if len(identities) > MAX_QUALITY_RUNS:
            reject("video_export_input_budget_exceeded", 422)
    frames = func.coalesce(
        VideoTrackQualityRun.metrics["sampling_frames"], literal([], type_=JSONB)
    )
    projection = (
        select(
            *(getattr(VideoTrackQualityRun, name) for name in _QUALITY_FIELDS),
            frames.label("sampling_frames"),
            func.jsonb_typeof(VideoTrackQualityRun.metrics).label("metrics_type"),
        )
        .where(predicate)
        .subquery("video_export_quality_projection")
    )
    encoded = cast(func.to_jsonb(projection.table_valued()), Text)
    measure = select(
        func.count().label("count"),
        func.coalesce(func.sum(func.octet_length(encoded)), 0).label("size"),
    ).select_from(projection)
    prior_size = budget.read_bytes
    remaining = MAX_JSON_BYTES - prior_size - budget.media_bytes
    count, size = (await db.execute(measure)).one()
    if count > MAX_QUALITY_RUNS or size > remaining:
        reject("video_export_input_budget_exceeded", 422)
    aggregate = measure.correlate(None).cte("video_export_quality_budget")
    # A concurrent update after measure cannot send a large sampling array or
    # pair list to Python: both this guard and the projection use one MVCC view.
    query = (
        select(projection, aggregate.c.count, aggregate.c.size)
        .select_from(aggregate)
        .outerjoin(
            projection,
            and_(
                aggregate.c.count <= MAX_QUALITY_RUNS,
                aggregate.c.size <= remaining,
            ),
        )
        .order_by(projection.c.created_at.desc(), projection.c.id.desc())
    )
    result = list((await db.execute(query)).mappings())
    actual_count, actual_size = result[0]["count"], result[0]["size"]
    budget.read_bytes = prior_size + int(actual_size)
    if actual_count > MAX_QUALITY_RUNS or actual_size > remaining:
        reject("video_export_input_budget_exceeded", 422)
    rows = []
    for facts in result:
        if facts["id"] is None:
            continue
        if facts["metrics_type"] != "object":
            reject("video_export_quality_invalid", 422)
        rows.append(
            SimpleNamespace(
                **{name: facts[name] for name in _QUALITY_FIELDS},
                metrics={"sampling_frames": facts["sampling_frames"]},
            )
        )
    if len(rows) != count or len(rows) != actual_count:
        reject()
    return rows


def _quality_facts(rows):
    result, pairs, samples = [], 0, 0
    for row in rows:
        if not isinstance(row.metrics, dict):
            reject("video_export_quality_invalid", 422)
        frames = row.metrics.get("sampling_frames", [])
        if (
            not isinstance(frames, list)
            or any(
                type(frame) is not int or not 0 <= frame < 1_000_000 for frame in frames
            )
            or not isinstance(row.pairs, list)
        ):
            reject("video_export_quality_invalid", 422)
        pairs += len(row.pairs)
        samples += len(frames)
        if pairs > MAX_QUALITY_PAIRS or samples > MAX_QUALITY_SAMPLE_FRAMES:
            reject("video_export_input_budget_exceeded", 422)
        result.append(
            {
                **lidar_inputs.row_value(row, fields=_QUALITY_FIELDS),
                "metrics": {"sampling_frames": deepcopy(frames)},
            }
        )
    return result


async def collect(
    db, project_id, batch_id, targets, options, *, task_ids=None, lock=False
):
    try:
        return await _collect(
            db, project_id, batch_id, targets, options, task_ids=task_ids, lock=lock
        )
    except DBAPIError as exc:
        if (
            storage_access.unavailable(exc)
            or getattr(exc.orig, "sqlstate", None) == "40001"
        ):
            reject("video_export_inputs_busy")
        raise


async def _collect(db, project_id, batch_id, targets, options, *, task_ids, lock):
    selected = _selection(task_ids)
    if (
        not isinstance(targets, list)
        or any(not isinstance(target, str) for target in targets)
        or len(set(targets)) != len(targets)
        or not isinstance(options, dict)
    ):
        reject("video_export_contract_missing", 422)
    scope = scope_from_dict(options.get("video_export_scope"))
    budget = _Budget()
    projects = await _rows(
        db,
        Project,
        Project.id == project_id,
        budget=budget,
        max_rows=1,
        lock=lock,
        write=True,
    )
    project = projects[0] if projects else None
    if project is None or not uses_contract(project, targets):
        reject("video_export_contract_missing", 422)
    predicate = Task.project_id == project_id
    if batch_id is not None:
        predicate &= Task.batch_id == batch_id
    if selected is not None:
        predicate &= ids_clause(Task.id, selected)
    if scope is not None:
        if selected is not None and selected != [scope.task_id]:
            reject("video_export_scope_changed")
        predicate &= Task.id == scope.task_id
    before = list(
        (
            await db.execute(
                select(Task.id, Task.batch_id)
                .where(predicate)
                .order_by(Task.id)
                .limit(MAX_TASKS + 1)
            )
        ).all()
    )
    if len(before) > MAX_TASKS:
        reject("video_export_input_budget_exceeded", 422)
    if (selected is not None and {row.id for row in before} != set(selected)) or (
        scope is not None and {row.id for row in before} != {scope.task_id}
    ):
        reject("video_export_scope_changed")
    batch_ids = {row.batch_id for row in before if row.batch_id} | (
        {batch_id} if batch_id else set()
    )
    batches = (
        await _rows(
            db,
            TaskBatch,
            ids_clause(TaskBatch.id, batch_ids),
            budget=budget,
            lock=lock,
            write=True,
        )
        if batch_ids
        else []
    )
    batch = next((row for row in batches if row.id == batch_id), None)
    if batch_id is not None and (batch is None or batch.project_id != project_id):
        reject("video_export_scope_changed")
    tasks = await _rows(
        db,
        Task,
        predicate,
        budget=budget,
        max_rows=MAX_TASKS,
        lock=lock,
        write=True,
        order=[Task.sequence_order, Task.created_at, Task.id],
    )
    if sorted((task.id, task.batch_id) for task in tasks) != [
        (row.id, row.batch_id) for row in before
    ]:
        reject()
    identities = [task.id for task in tasks]
    links = (
        await _rows(
            db,
            TaskDatasetItemLink,
            ids_clause(TaskDatasetItemLink.task_id, identities),
            budget=budget,
            lock=lock,
        )
        if identities
        else []
    )
    item_ids = {task.dataset_item_id for task in tasks if task.dataset_item_id} | {
        link.dataset_item_id for link in links
    }
    dataset_ids = (
        list(
            await db.scalars(
                select(DatasetItem.dataset_id)
                .where(ids_clause(DatasetItem.id, item_ids))
                .distinct()
            )
        )
        if item_ids
        else []
    )
    datasets = (
        await _rows(
            db, Dataset, ids_clause(Dataset.id, dataset_ids), budget=budget, lock=lock
        )
        if dataset_ids
        else []
    )
    items = (
        await _rows(
            db,
            DatasetItem,
            ids_clause(DatasetItem.id, item_ids),
            budget=budget,
            lock=lock,
            write=True,
        )
        if item_ids
        else []
    )
    if {item.id for item in items} != item_ids:
        reject()
    segments = (
        await _rows(
            db,
            VideoSegment,
            ids_clause(VideoSegment.dataset_item_id, item_ids),
            budget=budget,
            max_rows=MAX_SEGMENTS,
            lock=lock,
            order=[
                VideoSegment.dataset_item_id,
                VideoSegment.segment_index,
                VideoSegment.id,
            ],
        )
        if item_ids
        else []
    )
    # Quality freshness deliberately uses every raw annotation for a Task. All
    # inactive/cancelled rows stay locked against restoration at the final fence.
    all_annotations = (
        await _rows(
            db,
            Annotation,
            ids_clause(Annotation.task_id, identities),
            budget=budget,
            max_rows=MAX_ANNOTATIONS,
            lock=lock,
        )
        if identities
        else []
    )
    annotations = sorted(
        (row for row in all_annotations if row.is_active and not row.was_cancelled),
        key=lambda row: (row.created_at, row.id),
    )
    runs = (
        await _quality_rows(
            db,
            ids_clause(VideoTrackQualityRun.task_id, identities),
            budget=budget,
            lock=lock,
        )
        if identities
        else []
    )
    quality_runs = _quality_facts(runs)
    predictions, scenes, tracks, intervals = [], [], [], []
    if "aap_json" in targets and identities:
        predictions = await _rows(
            db,
            Prediction,
            (Prediction.project_id == project_id)
            & ids_clause(Prediction.task_id, identities),
            budget=budget,
            max_rows=MAX_PREDICTIONS,
            lock=lock,
            order=[Prediction.created_at, Prediction.id],
        )
        track_ids = {
            row.scene_track_id
            for row in annotations
            if row.project_id == project_id and row.scene_track_id
        }
        if track_ids:
            scene_ids = list(
                await db.scalars(
                    select(SceneTrack.scene_id)
                    .where(ids_clause(SceneTrack.id, track_ids))
                    .distinct()
                )
            )
            scenes = await _rows(
                db,
                Scene,
                ids_clause(Scene.id, scene_ids),
                budget=budget,
                lock=lock,
                write=True,
            )
            tracks = await _rows(
                db,
                SceneTrack,
                ids_clause(SceneTrack.id, track_ids),
                budget=budget,
                max_rows=MAX_ANNOTATIONS,
                lock=lock,
                write=True,
            )
            intervals = await _rows(
                db,
                SceneTrackInterval,
                ids_clause(SceneTrackInterval.scene_track_id, track_ids),
                budget=budget,
                max_rows=MAX_ANNOTATIONS,
                lock=lock,
                order=[
                    SceneTrackInterval.scene_track_id,
                    SceneTrackInterval.start_frame,
                    SceneTrackInterval.id,
                ],
            )
            if {row.id for row in tracks} != track_ids or {
                row.id for row in scenes
            } != set(scene_ids):
                reject()
    geometry_budget(annotations, predictions)
    classes = set(derive_classes_list(project.tool_bindings))
    exported = [
        row
        for row in annotations
        if row.project_id == project_id and row.class_name in classes
    ]
    expansion = _mask_budget(exported, predictions)
    if expansion + budget.read_bytes + budget.media_bytes > MAX_JSON_BYTES:
        reject("video_export_input_budget_exceeded", 422)
    resolver = original_sources.CachedResolver(db, lock=lock, max_links=MAX_LINKS)
    media, sources, unique, total_bytes = [], {}, {}, 0
    for task in tasks:
        if task.file_type != "video" or is_reference(task.file_path):
            reject("video_export_original_unsupported", 422)
        source = await resolver.task(task.id)
        if (
            source.owner_kind not in {"dataset_asset", "task_original"}
            or source.media_type != "video"
            or source.bucket is None
            or source.location_id is None
            or source.endpoint_url is None
            or type(source.size_bytes) is not int
            or not 0 < source.size_bytes <= MAX_OBJECT_BYTES
        ):
            reject("video_export_original_unsupported", 422)
        key, frozen_source = (
            lidar_inputs.source_key(source),
            lidar_inputs.source_value(source),
        )
        identity = {**frozen_source, "relation": None}
        if key in unique and unique[key] != identity:
            reject("video_export_original_changed")
        if key not in unique:
            unique[key] = identity
            total_bytes += source.size_bytes
        entry = {
            "task_id": str(task.id),
            "role": "video",
            "link_id": None,
            "source_key": key,
            "source": frozen_source,
        }
        media.append(entry)
        sources[f"{task.id}:video"] = source
        budget.extra(entry)
        if total_bytes > MAX_TOTAL_BYTES:
            reject("video_export_input_budget_exceeded", 422)
    if expansion + budget.read_bytes + budget.media_bytes > MAX_JSON_BYTES:
        reject("video_export_input_budget_exceeded", 422)
    row_value = lidar_inputs.row_value
    frozen = {
        "schema": CONTRACT,
        "probe_contract": PROBE_CONTRACT,
        "project_id": str(project_id),
        "batch_id": str(batch_id) if batch_id else None,
        "task_ids": [str(identifier) for identifier in selected]
        if selected is not None
        else None,
        "targets": list(targets),
        "options": lidar_inputs.value(deepcopy(options)),
        "project": row_value(project, fields=_PROJECT_FIELDS),
        "tasks": [row_value(row, fields=image_inputs._TASK_FIELDS) for row in tasks],
        "batch": row_value(batch, fields=("id", "project_id", "display_id"))
        if batch
        else None,
        "links": [row_value(row) for row in links],
        "datasets": [
            {
                **row_value(row, fields=("id", "name", "display_id")),
                "metadata_": {
                    "axis_convention": (row.metadata_ or {}).get("axis_convention")
                },
            }
            for row in datasets
        ],
        "items": [
            {
                **row_value(row, fields=image_inputs._ITEM_FIELDS),
                "metadata_": {
                    "storage_relative_path": (row.metadata_ or {}).get(
                        "storage_relative_path"
                    )
                },
            }
            for row in items
        ],
        "annotations": [row_value(row, ignored={"updated_at"}) for row in annotations],
        "predictions": [row_value(row) for row in predictions],
        "scenes": [row_value(row, fields=("id", "name")) for row in scenes],
        "tracks": [row_value(row, ignored={"updated_at"}) for row in tracks],
        "intervals": [row_value(row) for row in intervals],
        "segments": [
            row_value(
                row,
                fields=(
                    "id",
                    "dataset_item_id",
                    "segment_index",
                    "start_frame",
                    "end_frame",
                ),
            )
            for row in segments
        ],
        "quality_runs": quality_runs,
        "references": {},
        "media": media,
        "counts": {"tasks": len(tasks), "managed": len(unique), "references": 0},
        "bytes": {"originals": total_bytes, "portable_masks": expansion},
    }
    digest(frozen)
    frozen = await db.scalar(
        select(cast(bindparam("frozen_video_inputs", frozen, type_=JSONB), JSONB))
    )
    digest(frozen)
    logical_sources(frozen)
    # Validate all required boundaries with detached facts before allocation.
    FrozenVideo(frozen)
    return Inputs(frozen, sources)


async def check(db, snapshot, *, lock=False):
    try:
        if (
            not isinstance(snapshot, dict)
            or type(snapshot.get("schema")) is not int
            or snapshot["schema"] != CONTRACT
            or type(snapshot.get("probe_contract")) is not int
            or snapshot["probe_contract"] != PROBE_CONTRACT
        ):
            reject("video_export_contract_missing")
        current = await collect(
            db,
            uuid.UUID(snapshot["project_id"]),
            uuid.UUID(snapshot["batch_id"]) if snapshot["batch_id"] else None,
            snapshot["targets"],
            snapshot["options"],
            task_ids=snapshot["task_ids"],
            lock=lock,
        )
    except ContentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        reject("video_export_contract_missing")
    if current.digest != digest(snapshot):
        reject()
    return current


async def check_source(db, binding, *, lock=False):
    try:
        if binding["role"] != "video" or binding["link_id"] is not None:
            reject("video_export_original_changed")
        source = await original_sources.CachedResolver(
            db, lock=lock, max_links=MAX_LINKS
        ).task(uuid.UUID(binding["task_id"]))
        if (
            lidar_inputs.source_value(source) != binding["source"]
            or lidar_inputs.source_key(source) != binding["source_key"]
        ):
            reject("video_export_original_changed")
        return source
    except ContentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        reject("video_export_original_changed")
