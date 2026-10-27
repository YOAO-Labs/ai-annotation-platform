"""Freeze the actual LiDAR export inputs without performing original I/O.

The caller owns authority and transaction boundaries. Publication takes the
same parent locks as annotation, link and calibration writers, then compares
the complete frozen input. Ordinary workbench lock heartbeats are not input.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
import hashlib
import json
import uuid

from sqlalchemy import Text, and_, bindparam, case, cast, func, inspect, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene, SensorCalibrationRevision
from app.db.models.project import Project
from app.db.models.prediction import Prediction
from app.db.models.scene_pose import SceneFramePose
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.task_scope import ids_clause
from aap_workspace_contract import derive_classes_list

CONTRACT = 2
TARGETS = frozenset({"kitti", "pointmask", "coco-multicamera", "nuscenes"})
MAX_IMAGE_BYTES = MAX_PCD_BYTES = 256 * 1024 * 1024
MAX_IMAGE_COUNT = 20_000
MAX_PCD_COUNT = 1_000
MAX_IMAGES_TOTAL = 20 * 1024**3
MAX_PCD_TOTAL = 4 * 1024**3
MAX_ANNOTATIONS = 100_000
MAX_JSON_BYTES = 256 * 1024**2
MAX_INDICES = 32_000_000
MAX_POINTS = 100_000_000
MAX_LINKS = 50_000

_PROJECT_FIELDS = (
    "id",
    "display_id",
    "name",
    "type_key",
    "type_label",
    "data_type",
    "tool_bindings",
    "rendering_config",
    "annotation_guide",
)
_TASK_FIELDS = (
    "id",
    "project_id",
    "batch_id",
    "dataset_item_id",
    "original_id",
    "display_id",
    "file_name",
    "file_path",
    "file_type",
    "sequence_order",
    "created_at",
    "tags",
)
_ITEM_IGNORED = frozenset(
    {
        "updated_at",
        "thumbnail_path",
        "blurhash",
        "video_output_id",
        "video_building_output_id",
    }
)


def reject(code="lidar_export_inputs_changed", status=409):
    raise ContentError(code, status)


def value(raw):
    """Only immutable JSON facts cross a transaction or physical boundary."""
    if is_dataclass(raw):
        return value(asdict(raw))
    if isinstance(raw, dict):
        return {str(k): value(v) for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [value(v) for v in raw]
    if isinstance(raw, uuid.UUID):
        return str(raw)
    if isinstance(raw, (datetime, date)):
        return raw.isoformat()
    return raw


def digest(data, *, limit=MAX_JSON_BYTES):
    """Count the actual encoding while hashing; do not build a second huge JSON."""
    hashed = hashlib.sha256()
    count = 0
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False)
    try:
        for part in encoder.iterencode(data):
            chunk = part.encode("utf-8")
            count += len(chunk)
            if count > limit:
                reject("lidar_export_input_budget_exceeded", 422)
            hashed.update(chunk)
    except ContentError:
        raise
    except (TypeError, ValueError):
        reject("lidar_export_input_invalid", 422)
    return hashed.hexdigest()


def row_value(row, *, fields=None, ignored=frozenset()):
    names = fields or [p.key for p in inspect(type(row)).column_attrs]
    return {name: value(getattr(row, name)) for name in names if name not in ignored}


def source_value(source, link_id=None, *, relation_links=None):
    # The complete Task link collection is frozen once in snapshot.links. Each
    # object guard needs only its exact camera and the canonical primary link.
    result = value(
        {
            name: getattr(source, name)
            for name in source.__dataclass_fields__
            if name != "relation"
        }
    )
    result["relation"] = None
    relation = source.relation
    if relation is not None:
        result["relation"] = value(
            {
                name: getattr(relation, name)
                for name in relation.__dataclass_fields__
                if name != "links"
            }
        )
        links = (
            relation_links
            if relation_links is not None
            else [
                link
                for link in relation.links
                if link.id in {relation.primary_link_id, link_id}
            ]
        )
        result["relation"]["links"] = sorted(value(links), key=lambda item: item["id"])
    return result


def source_key(source):
    """Deduplicate one registered object, retaining each Task/link separately."""
    # Exclude the potentially large relation before dataclass expansion.
    result = value(
        {
            name: getattr(source, name)
            for name in source.__dataclass_fields__
            if name != "relation"
        }
    )
    from app.services.exporting.lidar_sources import canonical_identity

    return digest(canonical_identity(result))


def uses_contract(project, targets):
    return project.data_type == "lidar" and bool(TARGETS.intersection(targets))


@dataclass(frozen=True)
class Inputs:
    snapshot: dict
    sources: dict[str, original_sources.OriginalSource]

    @property
    def digest(self):
        return digest(self.snapshot)


def _locked(query, lock, *, write=False):
    if lock:
        query = query.with_for_update(read=not write, nowait=True)
    return query.execution_options(populate_existing=True)


@dataclass
class _Budget:
    read_bytes: int = 0
    media_bytes: int = 0

    async def measure(self, db, model, predicate, max_rows):
        # Aggregate in PostgreSQL before materializing any potentially large
        # JSONB row. Include all stored columns as a conservative read budget.
        encoded_row = cast(func.to_jsonb(model.__table__.table_valued()), Text)
        count, size = (
            await db.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(func.octet_length(encoded_row)), 0),
                )
                .select_from(model)
                .where(predicate)
            )
        ).one()
        self.read_bytes += int(size)
        if count > max_rows or self.read_bytes > MAX_JSON_BYTES:
            reject("lidar_export_input_budget_exceeded", 422)
        return int(count)

    def media(self, entry):
        # Full per-binding relation facts can repeat. Stop while constructing
        # the snapshot instead of first building an unbounded media document.
        self.media_bytes += encoded_size(entry)
        if self.media_bytes + self.read_bytes > MAX_JSON_BYTES:
            reject("lidar_export_input_budget_exceeded", 422)


def encoded_size(data):
    count = 0
    for part in json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, allow_nan=False
    ).iterencode(data):
        count += len(part.encode("utf-8"))
        if count > MAX_JSON_BYTES:
            reject("lidar_export_input_budget_exceeded", 422)
    return count


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
    if lock:
        # Lock only small IDs before measuring JSONB. Writers cannot enlarge a
        # measured row before hydration. Caller-owned parent locks fence inserts.
        identities = list(
            await db.scalars(
                _locked(
                    select(model.id)
                    .where(predicate)
                    .order_by(model.id)
                    .limit(max_rows + 1),
                    True,
                    write=write,
                )
            )
        )
        if len(identities) > max_rows:
            reject("lidar_export_input_budget_exceeded", 422)
    prior_size = budget.read_bytes
    count = await budget.measure(db, model, predicate, max_rows)
    encoded_row = cast(func.to_jsonb(model.__table__.table_valued()), Text)
    aggregate = (
        select(
            func.count().label("count"),
            func.coalesce(func.sum(func.octet_length(encoded_row)), 0).label("size"),
        )
        .select_from(model)
        .where(predicate)
        .correlate(None)
        .cte()
    )
    # The guard and hydration share one PostgreSQL statement/MVCC snapshot.
    # READ COMMITTED preflight cannot race a larger JSON update between them.
    # The left join returns the guard even when the table is empty or rejected.
    query = (
        select(model, aggregate.c.count, aggregate.c.size)
        .select_from(aggregate)
        .outerjoin(
            model,
            and_(
                predicate,
                aggregate.c.count <= max_rows,
                aggregate.c.size <= MAX_JSON_BYTES - prior_size,
            ),
        )
        .order_by(*(order or [model.id]))
    )
    result = list(await db.execute(query.execution_options(populate_existing=True)))
    actual_count, actual_size = result[0][1:]
    budget.read_bytes = prior_size + int(actual_size)
    if actual_count > max_rows or budget.read_bytes > MAX_JSON_BYTES:
        reject("lidar_export_input_budget_exceeded", 422)
    rows = [row[0] for row in result if row[0] is not None]
    if len(rows) != count or len(rows) != actual_count:
        reject()
    return rows


async def collect(
    db,
    project_id,
    batch_id,
    targets,
    options,
    *,
    lock=False,
    allow_missing_kitti_camera=False,
):
    """Collect DB facts only. ``lock`` is exclusively for a short final fence.

    ``allow_missing_kitti_camera`` lets preflight freeze the scope before the
    user has picked a KITTI camera, so it can report the project's real camera
    roles instead of discarding them with the required-selection error. Export
    jobs keep the strict default and still reject a missing selection.
    """
    try:
        return await _collect(
            db,
            project_id,
            batch_id,
            targets,
            options,
            lock=lock,
            allow_missing_kitti_camera=allow_missing_kitti_camera,
        )
    except DBAPIError as exc:
        if (
            storage_access.unavailable(exc)
            or getattr(exc.orig, "sqlstate", None) == "40001"
        ):
            reject("lidar_export_inputs_busy")
        raise


async def _collect(
    db,
    project_id,
    batch_id,
    targets,
    options,
    *,
    lock,
    allow_missing_kitti_camera=False,
):
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
        reject("lidar_export_contract_missing", 422)
    predicate = Task.project_id == project_id
    if batch_id is not None:
        predicate &= Task.batch_id == batch_id
    count = await db.scalar(select(func.count(Task.id)).where(predicate))
    pcd_required = bool({"kitti", "pointmask", "nuscenes"}.intersection(targets))
    if not count:
        reject("lidar_export_empty", 422)
    task_limit = MAX_PCD_COUNT if pcd_required else MAX_IMAGE_COUNT
    if count > task_limit:
        reject("lidar_export_input_budget_exceeded", 422)
    before = list(
        (
            await db.execute(
                select(Task.id, Task.batch_id)
                .where(predicate)
                .order_by(Task.id)
                .limit(task_limit + 1)
            )
        ).all()
    )
    if len(before) > task_limit:
        reject("lidar_export_input_budget_exceeded", 422)
    batch_ids = sorted(
        {bid for _, bid in before if bid} | ({batch_id} if batch_id else set()), key=str
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
    if batch_id is not None and (
        len(batches) != 1 or batches[0].project_id != project_id
    ):
        reject("export_scope_changed")
    tasks = await _rows(
        db,
        Task,
        predicate,
        budget=budget,
        max_rows=task_limit,
        lock=lock,
        write=True,
        order=[Task.sequence_order, Task.created_at, Task.id],
    )
    if sorted((task.id, task.batch_id) for task in tasks) != before:
        reject()
    task_ids = [task.id for task in tasks]
    link_predicate = ids_clause(TaskDatasetItemLink.task_id, task_ids)
    if (
        await db.scalar(
            select(func.count(TaskDatasetItemLink.id)).where(link_predicate)
        )
        > MAX_LINKS
    ):
        reject("lidar_export_input_budget_exceeded", 422)
    links = await _rows(
        db, TaskDatasetItemLink, link_predicate, budget=budget, lock=lock
    )
    by_task = defaultdict(list)
    for link in links:
        by_task[link.task_id].append(link)
    item_ids = {link.dataset_item_id for link in links} | {
        task.dataset_item_id for task in tasks if task.dataset_item_id
    }
    # Lock calibration's parents in its Dataset -> Item order. Dataset reads
    # intentionally do not impose a writable/unarchived lifecycle requirement.
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
    item_by_id = {item.id: item for item in items}
    if set(item_by_id) != item_ids:
        reject()
    annotation_predicate = (
        (Annotation.project_id == project_id)
        & ids_clause(Annotation.task_id, task_ids)
        & Annotation.is_active.is_(True)
        & Annotation.was_cancelled.is_(False)
    )
    if "pointmask" in targets:
        indices = Annotation.geometry["point_indices"]
        index_count = await db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                func.jsonb_typeof(indices) == "array",
                                func.jsonb_array_length(indices),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                )
            ).where(
                annotation_predicate,
                Annotation.geometry["type"].as_string() == "point_mask_3d",
            )
        )
        if index_count > MAX_INDICES:
            reject("lidar_export_input_budget_exceeded", 422)
    annotations = await _rows(
        db,
        Annotation,
        annotation_predicate,
        budget=budget,
        max_rows=MAX_ANNOTATIONS,
        lock=lock,
        order=[Annotation.created_at, Annotation.id],
    )
    revisions = (
        await _rows(
            db,
            SensorCalibrationRevision,
            ids_clause(SensorCalibrationRevision.dataset_item_id, item_ids),
            budget=budget,
            max_rows=MAX_ANNOTATIONS,
            lock=lock,
        )
        if item_ids
        else []
    )
    scene_ids = {item.scene_id for item in items if item.scene_id}
    scenes = (
        await _rows(
            db,
            Scene,
            ids_clause(Scene.id, scene_ids),
            budget=budget,
            lock=lock,
            write=True,
        )
        if scene_ids
        else []
    )
    frame_pairs = {
        (item.scene_id, item.frame_index)
        for item in items
        if item.scene_id and item.frame_index is not None
    }
    poses = (
        await _rows(
            db,
            SceneFramePose,
            or_(
                *(
                    and_(
                        SceneFramePose.scene_id == scene,
                        SceneFramePose.frame_index == frame,
                    )
                    for scene, frame in sorted(frame_pairs)
                )
            ),
            budget=budget,
            lock=lock,
        )
        if frame_pairs and "nuscenes" in targets
        else []
    )
    track_ids = {ann.scene_track_id for ann in annotations if ann.scene_track_id}
    track_predicate = ids_clause(SceneTrack.id, track_ids)
    if "nuscenes" in targets:
        track_predicate |= (SceneTrack.project_id == project_id) & ids_clause(
            SceneTrack.scene_id, scene_ids
        )
    tracks = (
        await _rows(
            db,
            SceneTrack,
            track_predicate,
            budget=budget,
            max_rows=MAX_ANNOTATIONS,
            lock=lock,
            write=True,
        )
        if track_ids or (scene_ids and "nuscenes" in targets)
        else []
    )
    intervals = (
        await _rows(
            db,
            SceneTrackInterval,
            ids_clause(
                SceneTrackInterval.scene_track_id, [track.id for track in tracks]
            ),
            budget=budget,
            max_rows=MAX_ANNOTATIONS,
            lock=lock,
        )
        if tracks
        else []
    )
    extra_scene_ids = {track.scene_id for track in tracks} - scene_ids
    if extra_scene_ids:
        scenes += await _rows(
            db,
            Scene,
            ids_clause(Scene.id, extra_scene_ids),
            budget=budget,
            lock=lock,
            write=True,
        )
        scenes.sort(key=lambda scene: str(scene.id))
    predictions = (
        await _rows(
            db,
            Prediction,
            (Prediction.project_id == project_id)
            & ids_clause(Prediction.task_id, task_ids),
            budget=budget,
            max_rows=MAX_ANNOTATIONS,
            lock=lock,
            order=[Prediction.created_at, Prediction.id],
        )
        if "aap_json" in targets
        else []
    )
    scope_tasks, scope_items, scope_links = [], [], []
    if "nuscenes" in targets and scene_ids:
        # Completeness is about the entire Scene, including members outside a
        # batch selection. Scene -> Item parent locks fence future frame/link
        # additions; a bounded Task set freezes current membership as well.
        scope_items = await _rows(
            db,
            DatasetItem,
            ids_clause(DatasetItem.scene_id, scene_ids)
            & (DatasetItem.file_type == "point_cloud"),
            budget=budget,
            max_rows=MAX_PCD_COUNT,
            lock=lock,
            write=True,
        )
        scope_item_ids = [item.id for item in scope_items]
        primary_members = select(TaskDatasetItemLink.task_id).where(
            TaskDatasetItemLink.role == "primary_lidar",
            ids_clause(TaskDatasetItemLink.dataset_item_id, scope_item_ids),
        )
        scope_predicate = (Task.project_id == project_id) & (
            ids_clause(Task.dataset_item_id, scope_item_ids)
            | Task.id.in_(primary_members)
        )
        scope_tasks = await _rows(
            db,
            Task,
            scope_predicate,
            budget=budget,
            max_rows=MAX_PCD_COUNT,
            lock=lock,
            write=True,
        )
        scope_links = await _rows(
            db,
            TaskDatasetItemLink,
            ids_clause(TaskDatasetItemLink.task_id, [task.id for task in scope_tasks])
            & (TaskDatasetItemLink.role == "primary_lidar"),
            budget=budget,
            max_rows=MAX_PCD_COUNT,
            lock=lock,
        )
        primary_by_task = {link.task_id: link.dataset_item_id for link in scope_links}
        for task in scope_tasks:
            primary = primary_by_task.get(task.id)
            if (task.original_id and (task.dataset_item_id or primary)) or (
                task.dataset_item_id and primary and task.dataset_item_id != primary
            ):
                reject("original_relation_conflict")
    sources, media = {}, []
    resolver = original_sources.CachedResolver(db, lock=lock, max_links=MAX_LINKS)
    relation_links_by_task = {}
    seen = {}
    counts = {kind: 0 for kind in ("image", "point_cloud", "raw_lidar", "map")}
    sizes = dict.fromkeys(counts, 0)
    selected_role = ((options or {}).get("lidar") or {}).get("kitti_camera_role")
    kitti_camera_pending = "kitti" in targets and not selected_role
    if kitti_camera_pending and not allow_missing_kitti_camera:
        reject("kitti_camera_required", 422)
    for task in tasks:
        requested = []
        if pcd_required:
            requested.append(("primary_lidar", None))
        for link in by_task[task.id]:
            if not link.role.startswith("camera_"):
                continue
            if {"pointmask", "coco-multicamera", "nuscenes"}.intersection(targets) or (
                "kitti" in targets and link.role == selected_role
            ):
                requested.append((link.role, link.id))
        if (
            "kitti" in targets
            and not kitti_camera_pending
            and not any(role == selected_role for role, _ in requested)
        ):
            reject("kitti_camera_role_not_found", 422)
        if "coco-multicamera" in targets and not any(
            link_id for _, link_id in requested
        ):
            reject("multicamera_coco_camera_missing", 422)
        if "nuscenes" in targets:
            requested.extend((("raw_lidar", None), ("map", None)))
        for role, link_id in requested:
            if role in {"raw_lidar", "map"}:
                from app.services import auxiliary_originals

                resolve = (
                    auxiliary_originals.resolve_task_raw
                    if role == "raw_lidar"
                    else auxiliary_originals.resolve_scene_map
                )
                source = await resolve(db, task.id, lock=lock)
            else:
                source = await (
                    resolver.link(task.id, link_id)
                    if link_id
                    else resolver.task(task.id)
                )
            if task.id not in relation_links_by_task:
                relation_links_by_task[task.id] = {
                    link.id: link for link in source.relation.links
                }
            relation_index = relation_links_by_task[task.id]
            compact_links = [
                relation_index[identifier]
                for identifier in {source.relation.primary_link_id, link_id}
                if identifier is not None and identifier in relation_index
            ]
            from app.services.exporting.lidar_sources import media_type

            expected_type = media_type(role)
            if (
                source.media_type != expected_type
                or source.bucket is None
                or source.location_id is None
                or source.endpoint_url is None
                or type(source.size_bytes) is not int
                or not (0 if expected_type == "raw_lidar" else 1)
                <= source.size_bytes
                <= MAX_IMAGE_BYTES
            ):
                reject("lidar_export_original_unsupported", 422)
            key = source_key(source)
            if key not in seen:
                seen[key] = expected_type
                counts[expected_type] += 1
                sizes[expected_type] += source.size_bytes
                if (
                    counts["image"] > MAX_IMAGE_COUNT
                    or counts["point_cloud"] > MAX_PCD_COUNT
                    or sizes["image"] > MAX_IMAGES_TOTAL
                    or sizes["point_cloud"] > MAX_PCD_TOTAL
                    or sum(counts.values()) > 21_000
                    or sum(sizes.values()) > MAX_IMAGES_TOTAL + MAX_PCD_TOTAL
                ):
                    reject("lidar_export_input_budget_exceeded", 422)
            label = f"{task.id}:{role}"
            if label in sources:
                reject("lidar_export_link_conflict")
            sources[label] = source
            entry = {
                "task_id": str(task.id),
                "role": role,
                "link_id": str(link_id) if link_id else None,
                "source_key": key,
                "source": source_value(source, link_id, relation_links=compact_links),
            }
            budget.media(entry)
            media.append(entry)
    if (
        counts["image"] > MAX_IMAGE_COUNT
        or counts["point_cloud"] > MAX_PCD_COUNT
        or sizes["image"] > MAX_IMAGES_TOTAL
        or sizes["point_cloud"] > MAX_PCD_TOTAL
        or sum(counts.values()) > 21_000
        or sum(sizes.values()) > MAX_IMAGES_TOTAL + MAX_PCD_TOTAL
    ):
        reject("lidar_export_input_budget_exceeded", 422)
    indices = 0
    if "pointmask" in targets:
        classes = set(derive_classes_list(project.tool_bindings))
        for ann in annotations:
            geometry = ann.geometry or {}
            if geometry.get("type") != "point_mask_3d":
                continue
            points = geometry.get("point_indices")
            if not isinstance(points, list) or ann.class_name not in classes:
                reject("lidar_export_pointmask_invalid", 422)
            indices += len(points)
            if indices > MAX_INDICES:
                reject("lidar_export_input_budget_exceeded", 422)
    frozen = {
        "schema": CONTRACT,
        "project_id": str(project_id),
        "batch_id": str(batch_id) if batch_id else None,
        "targets": list(dict.fromkeys(targets)),
        "options": value(deepcopy(options or {})),
        "project": row_value(project, fields=_PROJECT_FIELDS),
        "tasks": [row_value(task, fields=_TASK_FIELDS) for task in tasks],
        "batch": row_value(batches[0], fields=("id", "project_id", "display_id"))
        if batch_id
        else None,
        "links": [row_value(link) for link in links],
        "datasets": [
            row_value(
                ds,
                fields=(
                    "id",
                    "display_id",
                    "name",
                    "data_type",
                    "is_temporal",
                    "lifecycle_revision",
                    "archived_at",
                    "metadata_",
                ),
            )
            for ds in datasets
        ],
        "items": [row_value(item, ignored=_ITEM_IGNORED) for item in items],
        "annotations": [row_value(ann, ignored={"updated_at"}) for ann in annotations],
        "calibrations": [row_value(revision) for revision in revisions],
        "predictions": [row_value(prediction) for prediction in predictions],
        "scene_scope_tasks": [
            row_value(task, fields=_TASK_FIELDS) for task in scope_tasks
        ],
        "scene_scope_items": [
            row_value(
                item,
                fields=(
                    "id",
                    "dataset_id",
                    "file_path",
                    "file_type",
                    "frame_index",
                    "scene_id",
                    "metadata_",
                ),
            )
            for item in scope_items
        ],
        "scene_scope_links": [row_value(link) for link in scope_links],
        "scenes": [row_value(scene, ignored={"updated_at"}) for scene in scenes],
        "poses": [row_value(pose) for pose in poses],
        "tracks": [row_value(track, ignored={"updated_at"}) for track in tracks],
        "intervals": [
            row_value(interval, ignored={"updated_at"}) for interval in intervals
        ],
        "media": media,
        "counts": counts,
        "bytes": sizes,
    }
    digest(frozen)
    # Persisted JSONB normalizes numeric spellings (including negative zero).
    # Hash the same representation a later worker will read, not a pre-storage
    # Python spelling that could make a lawful frozen contract unverifiable.
    frozen = await db.scalar(
        select(cast(bindparam("frozen_inputs", frozen, type_=JSONB), JSONB))
    )
    digest(frozen)
    from app.services.exporting.lidar_media_paths import logical_sources

    try:
        logical_sources(frozen, allow_missing_kitti_camera=allow_missing_kitti_camera)
    except (KeyError, TypeError, ValueError):
        reject("lidar_export_archive_path_conflict", 422)
    return Inputs(frozen, sources)


async def check(db, snapshot, *, lock=False):
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("schema")) is not int
        or snapshot.get("schema") != CONTRACT
    ):
        reject("lidar_export_contract_missing")
    try:
        current = await collect(
            db,
            uuid.UUID(snapshot["project_id"]),
            uuid.UUID(snapshot["batch_id"]) if snapshot["batch_id"] else None,
            snapshot["targets"],
            snapshot["options"],
            lock=lock,
        )
    except ContentError:
        raise
    except (KeyError, TypeError, ValueError):
        reject("lidar_export_contract_missing")
    if current.digest != digest(snapshot):
        reject()
    return current


async def check_source(db, binding, *, lock=False):
    task_id = uuid.UUID(binding["task_id"])
    if binding["role"] in {"raw_lidar", "map"}:
        from app.services import auxiliary_originals

        resolve = (
            auxiliary_originals.resolve_task_raw
            if binding["role"] == "raw_lidar"
            else auxiliary_originals.resolve_scene_map
        )
        source = await resolve(db, task_id, lock=lock)
        if source_value(source) != binding["source"]:
            reject("lidar_export_original_changed")
        return source
    link_id = uuid.UUID(binding["link_id"]) if binding["link_id"] else None
    source = await (
        original_sources.resolve_task_link(
            db, task_id, link_id, lock=lock, compact_relation=True
        )
        if link_id
        else original_sources.resolve_task(
            db, task_id, lock=lock, compact_relation=True
        )
    )
    if source_value(source, link_id) != binding["source"]:
        reject("lidar_export_original_changed")
    return source
