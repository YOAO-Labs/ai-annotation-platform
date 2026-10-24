"""Serialize image formats from detached, receipt-bound business facts."""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import uuid

from sqlalchemy import DateTime, Uuid, inspect

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.schemas.aap_json import AAPSceneTrackEntry, AAPSceneTrackInterval
from app.services.content_snapshot import ContentError
from app.services.exporting.service import ExportService, _export_annotation_copy
from app.services.project import derive_attribute_keys, derive_classes_list


def _reject():
    raise ContentError("image_export_inputs_changed")


def _detached(model, facts):
    mapped = {entry.key: entry.columns[0].type for entry in inspect(model).column_attrs}
    if not isinstance(facts, dict) or not set(facts).issubset(mapped):
        _reject()
    values = {}
    try:
        for key, value in facts.items():
            column = mapped[key]
            if value is not None:
                if isinstance(column, Uuid):
                    value = uuid.UUID(str(value))
                elif isinstance(column, DateTime):
                    value = datetime.fromisoformat(value)
                else:
                    value = deepcopy(value)
            values[key] = value
        return model(**values)
    except (ValueError, TypeError, AttributeError):
        _reject()


class FrozenImage:
    """All serializer business reads are detached from the caller's session."""

    def __init__(self, snapshot):
        self.project_id = uuid.UUID(snapshot["project_id"])
        self.batch_id = (
            uuid.UUID(snapshot["batch_id"]) if snapshot["batch_id"] else None
        )
        self.batch_display_id = (snapshot.get("batch") or {}).get("display_id")
        self.targets = list(snapshot["targets"])
        self.project = _detached(Project, snapshot["project"])
        self.tasks = [_detached(Task, row) for row in snapshot["tasks"]]
        self.items = {
            row.id: row
            for row in (_detached(DatasetItem, row) for row in snapshot["items"])
        }
        self.datasets = {
            row.id: row
            for row in (_detached(Dataset, row) for row in snapshot["datasets"])
        }
        self.annotations = [
            _detached(Annotation, row) for row in snapshot["annotations"]
        ]
        self.predictions = [
            _detached(Prediction, row) for row in snapshot["predictions"]
        ]
        self.scenes_by_id = {
            row.id: row for row in (_detached(Scene, row) for row in snapshot["scenes"])
        }
        self.tracks = [_detached(SceneTrack, row) for row in snapshot["tracks"]]
        self.intervals = [
            _detached(SceneTrackInterval, row) for row in snapshot["intervals"]
        ]
        media_items = {
            uuid.UUID(row["task_id"]): uuid.UUID(row["source"]["item_id"])
            for row in snapshot["media"]
            if row["source"].get("item_id")
        }
        self.items_by_task = {
            task.id: self.items.get(media_items.get(task.id, task.dataset_item_id))
            for task in self.tasks
        }
        self._annotations_by_task = defaultdict(list)
        classes = set(derive_classes_list(self.project.tool_bindings))
        attributes = derive_attribute_keys(self.project.tool_bindings)
        for row in self.annotations:
            if row.class_name in classes:
                self._annotations_by_task[row.task_id].append(
                    _export_annotation_copy(row, attributes)
                )

    def axis_convention(self, task_id):
        item = self.items_by_task.get(task_id)
        dataset = self.datasets.get(item.dataset_id) if item else None
        return (dataset.metadata_ or {}).get("axis_convention") if dataset else None

    def service(self, db, *, media_paths=None):
        return FrozenExportService(db, self, media_paths=media_paths)


class FrozenExportService(ExportService):
    def __init__(self, db, frozen, *, media_paths=None):
        super().__init__(db, media_paths=media_paths)
        self.frozen = frozen

    def _scope(self, project_id, batch_id):
        if project_id != self.frozen.project_id or batch_id != self.frozen.batch_id:
            _reject()

    async def _load_data(self, project_id, batch_id=None, *, video_scope=None):
        self._scope(project_id, batch_id)
        if video_scope is not None:
            _reject()
        annotations = [
            ann
            for task in self.frozen.tasks
            for ann in self.frozen._annotations_by_task[task.id]
        ]
        return self.frozen.project, self.frozen.tasks, annotations

    async def _load_predictions(self, project_id, task_ids):
        if project_id != self.frozen.project_id:
            _reject()
        selected = set(task_ids)
        return [row for row in self.frozen.predictions if row.task_id in selected]

    async def _load_dataset_items(self, tasks):
        return {
            task.dataset_item_id: self.frozen.items[task.dataset_item_id]
            for task in tasks
            if task.dataset_item_id in self.frozen.items
        }

    def item_for_task(self, task, items):
        return self.frozen.items_by_task.get(task.id)

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
            _reject()
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
            _reject()
        return self.frozen.batch_display_id

    async def _aap_scene_tracks(self, annotations):
        selected = {ann.scene_track_id for ann in annotations if ann.scene_track_id}
        intervals = defaultdict(list)
        for row in sorted(
            self.frozen.intervals, key=lambda row: (row.scene_track_id, row.start_frame)
        ):
            intervals[row.scene_track_id].append(row)
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
