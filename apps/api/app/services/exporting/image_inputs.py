"""Bounded, metadata-only inputs for independent image export deliveries."""

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
import uuid

from sqlalchemy import bindparam, cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSource,
    DatasetSourceRevision,
)
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.storage_connection import StorageConnection
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError, MAX_BYTES, MAX_PIXELS
from app.services.exporting import lidar_inputs
from app.services.exporting.image_media_paths import TARGETS
from app.services.raster_mask_storage import collect_mask_references
from app.services.reference_keys import is_reference
from app.services.task_scope import ids_clause

CONTRACT = 3
MAX_TASKS = 20_000
MAX_LINKS = 50_000
MAX_ANNOTATIONS = MAX_PREDICTIONS = 100_000
MAX_OBJECT_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_JSON_BYTES = 256 * 1024**2

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
    "version",
)
_ITEM_FIELDS = (
    "id",
    "dataset_id",
    "file_name",
    "file_path",
    "file_type",
    "content_hash",
    "width",
    "height",
    "scene_id",
)


def reject(code="image_export_inputs_changed", status=409):
    raise ContentError(code, status)


def _translate(exc):
    if exc.code.startswith("lidar_export_"):
        reject(exc.code.replace("lidar_export_", "image_export_", 1), exc.status)
    raise exc


def digest(data):
    try:
        return lidar_inputs.digest(data, limit=MAX_JSON_BYTES)
    except ContentError as exc:
        _translate(exc)


def _size(data):
    try:
        return lidar_inputs.encoded_size(data)
    except ContentError as exc:
        _translate(exc)


def uses_contract(project, targets):
    return (
        project.data_type == "image"
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
        reject("image_export_scope_changed")
    try:
        result = sorted(
            {uuid.UUID(str(identifier)) for identifier in task_ids}, key=str
        )
    except (ValueError, TypeError, AttributeError):
        reject("image_export_scope_changed")
    if len(result) != len(task_ids):
        reject("image_export_scope_changed")
    return result


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
        self.media_bytes += _size(data)
        if self.media_bytes + self.read_bytes > MAX_JSON_BYTES:
            reject("image_export_input_budget_exceeded", 422)


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
    # Reuse the same-statement MVCC size guard. The shared helper meters full
    # stored rows conservatively; only consumed facts enter the image snapshot.
    try:
        rows = await lidar_inputs._rows(
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
        reject("image_export_input_budget_exceeded", 422)
    return rows


def _query(query, lock):
    return query.with_for_update(read=True, nowait=True) if lock else query


async def _reference(db, task, item, *, lock):
    """The reference_binding contract using only bounded source fact columns.

    No config, encrypted credential, root identity or unbounded reason text is
    materialized. In particular an unlocked recollect cannot race a large JSON
    replacement between an earlier measurement and an ORM populate_existing.
    """
    if item is None or item.file_path != task.file_path:
        reject("content_unverified")
    reference = (
        await db.execute(
            _query(
                select(
                    DatasetReference.source_id,
                    DatasetReference.run_id,
                ).where(DatasetReference.dataset_item_id == item.id),
                lock,
            )
        )
    ).one_or_none()
    asset = (
        await db.execute(
            _query(
                select(
                    DatasetAsset.id,
                    DatasetAsset.verification_state,
                    DatasetAsset.checksum_algorithm,
                    DatasetAsset.checksum_value,
                    DatasetAsset.size_bytes,
                ).where(DatasetAsset.dataset_item_id == item.id),
                lock,
            )
        )
    ).one_or_none()
    if (
        reference is None
        or asset is None
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
    ):
        reject("content_unverified")
    source = (
        await db.execute(
            _query(
                select(
                    DatasetSource.id,
                    DatasetSource.location_id,
                    DatasetSource.enabled,
                    DatasetSource.unregistered_at,
                    DatasetSource.config_revision,
                ).where(DatasetSource.id == reference.source_id),
                lock,
            )
        )
    ).one_or_none()
    if source is None or not source.enabled or source.unregistered_at is not None:
        reject("source_unavailable")
    location = (
        await db.execute(
            _query(
                select(
                    StorageLocation.id,
                    StorageLocation.enabled,
                    StorageLocation.config_revision,
                    StorageLocation.connection_id,
                ).where(StorageLocation.id == source.location_id),
                lock,
            )
        )
    ).one_or_none()
    if location is None or not location.enabled:
        reject("location_unavailable")
    connection = None
    if location.connection_id is not None:
        connection = (
            await db.execute(
                _query(
                    select(
                        StorageConnection.id,
                        StorageConnection.revision,
                    ).where(StorageConnection.id == location.connection_id),
                    lock,
                )
            )
        ).one_or_none()
    accepted_id = await db.scalar(
        select(DatasetSourceRevision.id)
        .where(
            DatasetSourceRevision.source_id == reference.source_id,
            DatasetSourceRevision.dataset_item_id == item.id,
            DatasetSourceRevision.asset_id == asset.id,
            DatasetSourceRevision.ownership_mode == "reference",
        )
        .order_by(DatasetSourceRevision.acceptance_order.desc())
        .limit(1)
    )
    result = {
        "dataset_id": str(item.dataset_id),
        "asset_id": str(asset.id),
        "sha256": asset.checksum_value,
        "size": asset.size_bytes,
        "width": item.width,
        "height": item.height,
        "run_id": str(reference.run_id),
        "accepted_revision_id": str(accepted_id) if accepted_id else None,
        "source_id": str(source.id),
        "source_revision": source.config_revision,
        "location_id": str(location.id),
        "location_revision": location.config_revision,
        "maintenance_revision": await storage_access.check(db, location.id, lock=lock),
        "connection_id": str(connection.id) if connection else None,
        "connection_revision": connection.revision if connection else None,
    }
    if (
        type(result["size"]) is not int
        or not 0 < result["size"] <= MAX_BYTES
        or type(result["width"]) is not int
        or result["width"] <= 0
        or type(result["height"]) is not int
        or result["height"] <= 0
        or result["width"] * result["height"] > MAX_PIXELS
    ):
        reject("image_export_original_unsupported", 422)
    return result


def _portable_masks(annotations, predictions, targets):
    """Reserve declared canonical RLE expansion before a Worker reads masks."""
    references = {}
    values = [row.geometry for row in annotations]
    if "aap_json" in targets:
        values.extend(row.result for row in predictions)
    for value in values:
        for reference in collect_mask_references(value):
            key = reference.get("object_key")
            if not isinstance(key, str):
                reject("image_export_mask_invalid", 422)
            if key in references and references[key] != reference:
                reject("image_export_mask_invalid", 422)
            references[key] = reference
    expanded = 0
    for reference in references.values():
        size = reference.get("bytes")
        if type(size) is not int or not 0 < size <= 4 * 1024**2:
            reject("image_export_mask_invalid", 422)
        expanded += size
        if expanded > MAX_JSON_BYTES:
            reject("image_export_input_budget_exceeded", 422)
    return expanded


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
            reject("image_export_inputs_busy")
        raise


async def _collect(db, project_id, batch_id, targets, options, *, task_ids, lock):
    selected = _selection(task_ids)
    if (
        not isinstance(targets, list)
        or any(not isinstance(target, str) for target in targets)
        or len(set(targets)) != len(targets)
        or not isinstance(options, dict)
    ):
        reject("image_export_contract_missing", 422)
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
        reject("image_export_contract_missing", 422)
    predicate = Task.project_id == project_id
    if batch_id is not None:
        predicate &= Task.batch_id == batch_id
    if selected is not None:
        predicate &= ids_clause(Task.id, selected)
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
        reject("image_export_input_budget_exceeded", 422)
    if selected is not None and {row.id for row in before} != set(selected):
        reject("image_export_scope_changed")
    batch_ids = sorted(
        {row.batch_id for row in before if row.batch_id}
        | ({batch_id} if batch_id else set()),
        key=str,
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
        reject("image_export_scope_changed")
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
    item_index = {item.id: item for item in items}
    if set(item_index) != item_ids:
        reject()
    # Lock every relevant annotation, including inactive/cancelled rows, so a
    # restore/reclassification cannot enter the exported set after the check.
    all_annotations = (
        await _rows(
            db,
            Annotation,
            (Annotation.project_id == project_id)
            & ids_clause(Annotation.task_id, identities),
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
    predictions = []
    tracks = intervals = scenes = []
    if "aap_json" in targets:
        predictions = (
            await _rows(
                db,
                Prediction,
                (Prediction.project_id == project_id)
                & ids_clause(Prediction.task_id, identities),
                budget=budget,
                max_rows=MAX_PREDICTIONS,
                lock=lock,
                order=[Prediction.created_at, Prediction.id],
            )
            if identities
            else []
        )
        track_ids = {row.scene_track_id for row in annotations if row.scene_track_id}
        scene_ids = (
            list(
                await db.scalars(
                    select(SceneTrack.scene_id)
                    .where(ids_clause(SceneTrack.id, track_ids))
                    .distinct()
                )
            )
            if track_ids
            else []
        )
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
        tracks = (
            await _rows(
                db,
                SceneTrack,
                ids_clause(SceneTrack.id, track_ids),
                budget=budget,
                max_rows=MAX_ANNOTATIONS,
                lock=lock,
                write=True,
            )
            if track_ids
            else []
        )
        intervals = (
            await _rows(
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
            if track_ids
            else []
        )
        if {row.id for row in tracks} != track_ids or {row.id for row in scenes} != set(
            scene_ids
        ):
            reject()
    expansion = _portable_masks(annotations, predictions, targets)
    if expansion + budget.read_bytes > MAX_JSON_BYTES:
        reject("image_export_input_budget_exceeded", 422)
    resolver = original_sources.CachedResolver(db, lock=lock, max_links=MAX_LINKS)
    references, media, sources, unique = {}, [], {}, {}
    total_bytes = 0
    for task in tasks:
        if is_reference(task.file_path):
            reference = await _reference(
                db, task, item_index.get(task.dataset_item_id), lock=lock
            )
            references[str(task.id)] = reference
            budget.extra(reference)
            total_bytes += reference["size"]
        else:
            source = await resolver.task(task.id)
            if (
                source.media_type != "image"
                or source.bucket is None
                or source.location_id is None
                or source.endpoint_url is None
                or type(source.size_bytes) is not int
                or not 0 < source.size_bytes <= MAX_OBJECT_BYTES
            ):
                reject("image_export_original_unsupported", 422)
            key = lidar_inputs.source_key(source)
            frozen_source = lidar_inputs.source_value(source)
            identity = {**frozen_source, "relation": None}
            if key in unique and unique[key] != identity:
                reject("image_export_original_changed")
            if key not in unique:
                unique[key] = identity
                total_bytes += source.size_bytes
            entry = {
                "task_id": str(task.id),
                "role": "image",
                "link_id": None,
                "source_key": key,
                "source": frozen_source,
            }
            media.append(entry)
            sources[f"{task.id}:image"] = source
            budget.extra(entry)
        if total_bytes > MAX_TOTAL_BYTES:
            reject("image_export_input_budget_exceeded", 422)
    value, row_value = lidar_inputs.value, lidar_inputs.row_value
    frozen = {
        "schema": CONTRACT,
        "project_id": str(project_id),
        "batch_id": str(batch_id) if batch_id else None,
        "task_ids": [str(identifier) for identifier in selected]
        if selected is not None
        else None,
        "targets": list(targets),
        "options": value(deepcopy(options)),
        "project": row_value(project, fields=_PROJECT_FIELDS),
        "tasks": [row_value(row, fields=_TASK_FIELDS) for row in tasks],
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
                **row_value(row, fields=_ITEM_FIELDS),
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
        "references": references,
        "media": media,
        "counts": {
            "tasks": len(tasks),
            "managed": len(unique),
            "references": len(references),
        },
        "bytes": {"originals": total_bytes, "portable_masks": expansion},
    }
    digest(frozen)
    frozen = await db.scalar(
        select(cast(bindparam("frozen_image_inputs", frozen, type_=JSONB), JSONB))
    )
    digest(frozen)
    from app.services.exporting.image_media_paths import logical_sources

    logical_sources(frozen)
    return Inputs(frozen, sources)


async def check(db, snapshot, *, lock=False):
    try:
        if (
            not isinstance(snapshot, dict)
            or type(snapshot.get("schema")) is not int
            or snapshot["schema"] != CONTRACT
        ):
            reject("image_export_contract_missing")
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
        reject("image_export_contract_missing")
    if current.digest != digest(snapshot):
        reject()
    return current


async def check_source(db, binding, *, lock=False):
    try:
        if binding["role"] != "image" or binding["link_id"] is not None:
            reject("image_export_original_changed")
        source = await original_sources.CachedResolver(
            db, lock=lock, max_links=MAX_LINKS
        ).task(uuid.UUID(binding["task_id"]))
        if (
            lidar_inputs.source_value(source) != binding["source"]
            or lidar_inputs.source_key(source) != binding["source_key"]
        ):
            reject("image_export_original_changed")
        return source
    except ContentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        reject("image_export_original_changed")
