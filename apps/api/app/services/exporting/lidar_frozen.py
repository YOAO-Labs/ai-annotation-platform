"""Serialize LiDAR archives from frozen rows, never from changing business rows."""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import uuid

from sqlalchemy import DateTime, Uuid, inspect

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene, SensorCalibrationRevision
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_pose import SceneFramePose
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.aap_json import AAPSceneTrackEntry, AAPSceneTrackInterval
from app.services.content_snapshot import ContentError
from app.services.exporting.service import ExportService, _export_annotation_copy
from app.services.project import derive_attribute_keys, derive_classes_list
from app.services.sensor_calibration import (
    SensorCalibrationState,
    _metadata_calibration,
    calibration_digest,
)


def detached(model, facts):
    """Rehydrate only typed mapped values, with no session or lazy load owners."""
    mapped = {entry.key: entry.columns[0].type for entry in inspect(model).column_attrs}
    if not isinstance(facts, dict) or not set(facts).issubset(mapped):
        raise ContentError("lidar_export_contract_missing")
    values = {}
    for key, value in facts.items():
        column = mapped[key]
        if value is not None:
            if isinstance(column, Uuid):
                value = uuid.UUID(value)
            elif isinstance(column, DateTime):
                value = datetime.fromisoformat(value)
            else:
                value = deepcopy(value)
        values[key] = value
    return model(**values)


class FrozenLidar:
    """Read-only business view; a delivery context adds verified media receipts."""

    def __init__(self, snapshot):
        self.project_id = uuid.UUID(snapshot["project_id"])
        self.batch_id = (
            uuid.UUID(snapshot["batch_id"]) if snapshot["batch_id"] else None
        )
        self.batch_display_id = (snapshot.get("batch") or {}).get("display_id")
        self.project = detached(Project, snapshot["project"])
        self.tasks = [detached(Task, row) for row in snapshot["tasks"]]
        self.items = {
            item.id: item
            for item in (detached(DatasetItem, row) for row in snapshot["items"])
        }
        self.datasets = {
            row.id: row
            for row in (detached(Dataset, row) for row in snapshot["datasets"])
        }
        self.annotations = [
            detached(Annotation, row) for row in snapshot["annotations"]
        ]
        self.predictions = [
            detached(Prediction, row) for row in snapshot.get("predictions", [])
        ]
        self.scenes_by_id = {
            row.id: row for row in (detached(Scene, row) for row in snapshot["scenes"])
        }
        self.poses_by_frame = {
            (row.scene_id, row.frame_index): row
            for row in (detached(SceneFramePose, row) for row in snapshot["poses"])
        }
        self.tracks = [detached(SceneTrack, row) for row in snapshot["tracks"]]
        self.intervals = [
            detached(SceneTrackInterval, row) for row in snapshot["intervals"]
        ]
        self.calibrations = [
            detached(SensorCalibrationRevision, row) for row in snapshot["calibrations"]
        ]
        self.links_by_task = defaultdict(dict)
        self.camera_members_by_task = defaultdict(list)
        self._annotations_by_task = defaultdict(list)
        self._primary_by_task = {}
        for facts in snapshot["links"]:
            link = detached(TaskDatasetItemLink, facts)
            item = self.items.get(link.dataset_item_id)
            if item is None or link.role in self.links_by_task[link.task_id]:
                raise ContentError("lidar_export_link_conflict")
            self.links_by_task[link.task_id][link.role] = (link, item)
        for task in self.tasks:
            primary = self.links_by_task[task.id].get("primary_lidar")
            self._primary_by_task[task.id] = (
                primary[1] if primary else self.items.get(task.dataset_item_id)
            )
        classes = set(derive_classes_list(self.project.tool_bindings))
        attributes = derive_attribute_keys(self.project.tool_bindings)
        for row in self.annotations:
            if (
                row.sensor_role is not None
                and (row.geometry or {}).get("type") == "bbox"
            ):
                self.camera_members_by_task[row.task_id].append(row)
            if row.class_name in classes:
                self._annotations_by_task[row.task_id].append(
                    _export_annotation_copy(row, attributes)
                )
        for members in self.camera_members_by_task.values():
            members.sort(key=lambda row: row.id)
        self.all_scene_tasks = defaultdict(list)
        scope_items = {
            item.id: item
            for item in (
                detached(DatasetItem, row)
                for row in snapshot.get("scene_scope_items", [])
            )
        }
        scope_links = {
            row["task_id"]: uuid.UUID(row["dataset_item_id"])
            for row in snapshot.get("scene_scope_links", [])
        }
        for facts in snapshot.get("scene_scope_tasks", []):
            task = detached(Task, facts)
            identifier = task.dataset_item_id or scope_links.get(str(task.id))
            item = scope_items.get(identifier)
            if item is None:
                raise ContentError("lidar_export_inputs_changed")
            self.all_scene_tasks[item.scene_id].append((task, item))
        self._calibration_states = None

    def axis_convention(self, task_id):
        item = self._primary_by_task.get(uuid.UUID(str(task_id)))
        dataset = self.datasets.get(item.dataset_id) if item else None
        return (dataset.metadata_ or {}).get("axis_convention") if dataset else None

    @property
    def calibration_states(self):
        if self._calibration_states is None:
            latest = {}
            for row in sorted(
                self.calibrations, key=lambda row: row.revision, reverse=True
            ):
                latest.setdefault(row.dataset_item_id, row)
            states = {}
            # Match the existing COCO consumer: only manual camera bboxes need
            # current calibration facts. Negative images need no calibration.
            camera_ids = {
                item.id
                for linked in self.links_by_task.values()
                for role, (_, item) in linked.items()
                if role.startswith("camera_")
            }
            needed = {
                member.sensor_dataset_item_id
                for members in self.camera_members_by_task.values()
                for member in members
                if member.source == "manual"
                and member.sensor_dataset_item_id in camera_ids
            }
            for identifier in needed:
                item = self.items.get(identifier)
                if item is None:
                    continue
                row = latest.get(identifier)
                if row is None:
                    calibration = _metadata_calibration(item)
                    states[identifier] = SensorCalibrationState(
                        identifier, calibration, 1, calibration_digest(calibration)
                    )
                else:
                    states[identifier] = SensorCalibrationState(
                        identifier,
                        SensorCalibration.model_validate(row.calibration),
                        row.revision,
                        row.digest,
                        row.created_at,
                    )
            self._calibration_states = states
        return self._calibration_states

    def service(self, db):
        return FrozenExportService(db, self)


class FrozenExportService(ExportService):
    def __init__(self, db, frozen):
        super().__init__(db)
        self.frozen = frozen

    def _scope(self, project_id, batch_id):
        if project_id != self.frozen.project_id or batch_id != self.frozen.batch_id:
            raise ContentError("lidar_export_inputs_changed")

    async def _load_data(self, project_id, batch_id=None, *, video_scope=None):
        self._scope(project_id, batch_id)
        if video_scope is not None:
            raise ContentError("lidar_export_inputs_changed")
        annotations = [
            ann
            for task in self.frozen.tasks
            for ann in self.frozen._annotations_by_task[task.id]
        ]
        return self.frozen.project, self.frozen.tasks, annotations

    async def _load_predictions(self, project_id, task_ids):
        if project_id != self.frozen.project_id:
            raise ContentError("lidar_export_inputs_changed")
        selected = set(task_ids)
        return [row for row in self.frozen.predictions if row.task_id in selected]

    async def _load_dataset_items(self, tasks):
        return {
            task.dataset_item_id: self.frozen.items[task.dataset_item_id]
            for task in tasks
            if task.dataset_item_id in self.frozen.items
        }

    async def _axis_convention_by_task(self, tasks):
        return {task.id: self.frozen.axis_convention(task.id) for task in tasks}

    async def iter_export_chunks(
        self,
        project_id,
        batch_id=None,
        *,
        chunk_size=1000,
        with_annotations=True,
        video_scope=None,
    ):
        self._scope(project_id, batch_id)
        if (
            video_scope is not None
            or type(chunk_size) is not int
            or not 0 < chunk_size <= 1000
        ):
            raise ContentError("lidar_export_inputs_changed")
        for start in range(0, len(self.frozen.tasks), chunk_size):
            tasks = self.frozen.tasks[start : start + chunk_size]
            annotations = (
                {task.id: self.frozen._annotations_by_task[task.id] for task in tasks}
                if with_annotations
                else {}
            )
            yield tasks, annotations, await self._load_dataset_items(tasks)

    async def _aap_batch_display_id(self, batch_id):
        if batch_id != self.frozen.batch_id:
            raise ContentError("lidar_export_inputs_changed")
        return self.frozen.batch_display_id

    async def _aap_scene_tracks(self, annotations):
        selected = {ann.scene_track_id for ann in annotations if ann.scene_track_id}
        intervals = defaultdict(list)
        for interval in sorted(
            self.frozen.intervals, key=lambda row: (row.scene_track_id, row.start_frame)
        ):
            intervals[interval.scene_track_id].append(interval)
        tracks = [
            row
            for row in self.frozen.tracks
            if row.id in selected and row.scene_id in self.frozen.scenes_by_id
        ]
        tracks.sort(
            key=lambda row: (self.frozen.scenes_by_id[row.scene_id].name, row.track_id)
        )
        return [
            AAPSceneTrackEntry(
                track_id=row.track_id,
                scene_name=self.frozen.scenes_by_id[row.scene_id].name,
                class_name=row.class_name,
                presence_mode=row.presence_mode,
                attributes=row.attributes or {},
                attributes_meta=row.attributes_meta or {},
                intervals=[
                    AAPSceneTrackInterval(
                        start_frame=interval.start_frame,
                        end_frame=interval.end_frame,
                        source=interval.source,
                    )
                    for interval in intervals[row.id]
                ],
            )
            for row in tracks
        ]
