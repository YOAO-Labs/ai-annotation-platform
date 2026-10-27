"""Replay video boundaries and serialize exclusively from detached input facts."""

from collections import defaultdict
from copy import copy, deepcopy
from datetime import datetime
from types import SimpleNamespace
import uuid

from app.db.models.dataset import VideoSegment
from app.services.content_snapshot import ContentError
from app.services.exporting import image_frozen
from app.services.exporting.contracts import source_contract
from app.services.exporting.service import _export_annotation_copy
from app.services.exporting.video_scope import VideoExportScope, clip_video_geometry
from aap_workspace_contract import derive_attribute_keys, derive_classes_list
from app.services.video_canonical import (
    VideoBoundaryUnreconciledError,
    _core_scope,
    _required_boundaries,
    merge_canonical_annotations,
)
from app.services.video_collaboration import collaboration_enabled
from app.services.video_frame_service import derive_sampled_frames, derive_step
from app.services.video_track_quality import _snapshot
from app.services.mask_formats.contracts import canonical_digest


def reject(code="video_export_inputs_changed", status=409):
    raise ContentError(code, status)


def scope_from_dict(payload):
    """Read an already-normalized scope without coercing booleans or bad ranges."""
    if payload is None:
        return None
    if (
        not isinstance(payload, dict)
        or not {
            "task_id",
            "dataset_item_id",
            "selection_kind",
            "from_frame",
            "to_frame",
        }.issubset(payload)
        or set(payload)
        - {
            "task_id",
            "dataset_item_id",
            "selection_kind",
            "from_frame",
            "to_frame",
            "segments",
        }
        or payload["selection_kind"] not in {"frames", "segments"}
        or type(payload["from_frame"]) is not int
        or type(payload["to_frame"]) is not int
        or not 0 <= payload["from_frame"] <= payload["to_frame"] < 1_000_000
        or not isinstance(payload.get("segments", []), list)
    ):
        reject("video_export_scope_changed")
    try:
        from app.services.exporting.video_scope import VideoExportSegment

        segments = []
        for entry in payload.get("segments", []):
            if (
                not isinstance(entry, dict)
                or set(entry) != {"id", "segment_index", "start_frame", "end_frame"}
                or any(
                    type(entry[name]) is not int
                    for name in ("segment_index", "start_frame", "end_frame")
                )
                or entry["segment_index"] < 0
                or not 0 <= entry["start_frame"] <= entry["end_frame"] < 1_000_000
            ):
                reject("video_export_scope_changed")
            segments.append(
                VideoExportSegment(
                    uuid.UUID(entry["id"]),
                    entry["segment_index"],
                    entry["start_frame"],
                    entry["end_frame"],
                )
            )
        if (payload["selection_kind"] == "segments") != bool(segments):
            reject("video_export_scope_changed")
        return VideoExportScope(
            task_id=uuid.UUID(payload["task_id"]),
            dataset_item_id=uuid.UUID(payload["dataset_item_id"])
            if payload["dataset_item_id"] is not None
            else None,
            selection_kind=payload["selection_kind"],
            from_frame=payload["from_frame"],
            to_frame=payload["to_frame"],
            segments=tuple(segments),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        reject("video_export_scope_changed")


def _run(facts):
    try:
        return SimpleNamespace(
            id=uuid.UUID(facts["id"]),
            task_id=uuid.UUID(facts["task_id"]),
            left_segment_id=uuid.UUID(facts["left_segment_id"]),
            right_segment_id=uuid.UUID(facts["right_segment_id"]),
            created_at=datetime.fromisoformat(facts["created_at"]),
            status=facts["status"],
            input_digest=facts["input_digest"],
            sampling_digest=facts["sampling_digest"],
            metrics=deepcopy(facts["metrics"]),
            pairs=deepcopy(facts["pairs"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        reject("video_export_quality_invalid")


class FrozenVideo(image_frozen.FrozenImage):
    """Raw annotations stay available for freshness; outputs use pure projection."""

    def __init__(self, snapshot):
        if (
            snapshot.get("schema") != 4
            or type(snapshot.get("probe_contract")) is not int
            or snapshot["probe_contract"] != 1
        ):
            reject("video_export_contract_missing")
        try:
            super().__init__(snapshot)
            self.source_scope = self.video_scope = scope_from_dict(
                snapshot["options"].get("video_export_scope")
            )
            self.segments = [
                image_frozen._detached(VideoSegment, row)
                for row in snapshot["segments"]
            ]
            self.quality_runs = [_run(row) for row in snapshot["quality_runs"]]
            self.sources_by_task = {
                uuid.UUID(row["task_id"]): deepcopy(row["source"])
                for row in snapshot["media"]
            }
        except ContentError as exc:
            if exc.code.startswith("image_export_"):
                reject()
            raise
        except (KeyError, ValueError, TypeError, AttributeError):
            reject("video_export_contract_missing")
        self.metadata_by_task = {}
        self.metadata_receipts = {}
        self._validate_scope()
        self._segments_by_item = defaultdict(list)
        for segment in sorted(
            self.segments, key=lambda row: (row.segment_index, row.id)
        ):
            self._segments_by_item[segment.dataset_item_id].append(segment)
        self._runs_by_task = defaultdict(list)
        for run in sorted(
            self.quality_runs, key=lambda row: (row.created_at, row.id), reverse=True
        ):
            self._runs_by_task[run.task_id].append(run)
        self._raw_by_task_segment = defaultdict(list)
        for row in sorted(
            self.annotations, key=lambda row: (str(row.video_segment_id), row.id)
        ):
            self._raw_by_task_segment[(row.task_id, row.video_segment_id)].append(row)
        self._freshness = {}
        self._raw_quality_facts = {}
        self._freshness_work = 0
        self.selected_quality_runs = {}
        self._annotations_by_task = defaultdict(list)
        self._filtered_annotations = []
        classes = set(derive_classes_list(self.project.tool_bindings))
        attributes = derive_attribute_keys(self.project.tool_bindings)
        raw_by_task = defaultdict(list)
        for row in self.annotations:
            if row.project_id == self.project_id and row.class_name in classes:
                raw_by_task[row.task_id].append(
                    _export_annotation_copy(row, attributes)
                )
        for task in self.tasks:
            projected = self._canonical(task, raw_by_task[task.id])
            self._annotations_by_task[task.id] = projected
            self._filtered_annotations.extend(projected)

    def _validate_scope(self):
        scope = self.source_scope
        if scope is None:
            return
        tasks = {task.id: task for task in self.tasks}
        if set(tasks) != {scope.task_id}:
            reject("video_export_scope_changed")
        source = self.sources_by_task.get(scope.task_id)
        expected_item = source.get("item_id") if source else None
        if expected_item != (
            str(scope.dataset_item_id) if scope.dataset_item_id else None
        ):
            reject("video_export_scope_changed")
        if scope.selection_kind == "segments":
            if scope.dataset_item_id is None:
                reject("video_export_scope_changed")
            actual = {
                row.id: row
                for row in self.segments
                if row.dataset_item_id == scope.dataset_item_id
            }
            for expected in scope.segments:
                row = actual.get(expected.id)
                if row is None or any(
                    getattr(row, name) != getattr(expected, name)
                    for name in ("segment_index", "start_frame", "end_frame")
                ):
                    reject("video_export_scope_changed")
            if (
                any(
                    row.segment_index != scope.segments[0].segment_index + offset
                    for offset, row in enumerate(scope.segments)
                )
                or scope.from_frame != min(row.start_frame for row in scope.segments)
                or scope.to_frame != max(row.end_frame for row in scope.segments)
            ):
                reject("video_export_scope_changed")

    def _fresh(self, run):
        frames = run.metrics.get("sampling_frames") or []
        sample_digest = canonical_digest(frames)
        key = (run.task_id, run.left_segment_id, run.right_segment_id, sample_digest)
        if key not in self._freshness:
            self._freshness_work += len(
                self._raw_by_task_segment[(run.task_id, run.left_segment_id)]
            ) + len(self._raw_by_task_segment[(run.task_id, run.right_segment_id)])
            if self._freshness_work > 10_000_000:
                reject("video_export_input_budget_exceeded", 422)
            rows = sorted(
                self._raw_by_task_segment[(run.task_id, run.left_segment_id)]
                + self._raw_by_task_segment[(run.task_id, run.right_segment_id)],
                key=lambda row: (row.video_segment_id, row.id),
            )
            facts = []
            for row in rows:
                if row.id not in self._raw_quality_facts:
                    self._raw_quality_facts[row.id] = _snapshot([row])[0]
                facts.append(self._raw_quality_facts[row.id])
            self._freshness[key] = canonical_digest(
                {"annotations": facts, "sampling_digest": sample_digest}
            )
        return self._freshness[key] == run.input_digest

    def _accepted(self, task, segments):
        boundaries = _required_boundaries(segments, self.source_scope)
        runs, missing = [], []
        candidates_by_boundary = defaultdict(list)
        for run in self._runs_by_task[task.id]:
            candidates_by_boundary[(run.left_segment_id, run.right_segment_id)].append(
                run
            )
        for left, right in boundaries:
            candidates = candidates_by_boundary[(left.id, right.id)]
            selected = next(
                (
                    run
                    for run in candidates
                    if run.status in {"accepted", "empty_overlap"} and self._fresh(run)
                ),
                None,
            )
            if selected is None:
                newest = candidates[0] if candidates else None
                status = newest.status if newest else "missing"
                if (
                    newest
                    and status not in {"pending", "running", "failed", "stale"}
                    and not self._fresh(newest)
                ):
                    status = "stale"
                missing.append(
                    {
                        "left_segment_id": str(left.id),
                        "right_segment_id": str(right.id),
                        "run_id": str(newest.id) if newest else None,
                        "status": status,
                    }
                )
            else:
                runs.append(selected)
        if missing:
            raise VideoBoundaryUnreconciledError(missing)
        self.selected_quality_runs[task.id] = tuple(run.id for run in runs)
        return runs

    def _canonical(self, task, annotations):
        if collaboration_enabled(self.project):
            item = self.items_by_task.get(task.id)
            segments = self._segments_by_item[item.id] if item else []
            by_id, clipped = {row.id: row for row in segments}, []
            for annotation in annotations:
                segment = by_id.get(annotation.video_segment_id)
                if segment is None:
                    reject("video_export_annotation_segment_invalid")
                geometry = clip_video_geometry(
                    annotation.geometry, _core_scope(task, segment)
                )
                if geometry is not None:
                    row = copy(annotation)
                    row.geometry = geometry
                    clipped.append(row)
            annotations = merge_canonical_annotations(
                clipped, self._accepted(task, segments)
            )
        if self.source_scope is None:
            return annotations
        scoped = []
        for annotation in annotations:
            geometry = clip_video_geometry(annotation.geometry, self.source_scope)
            if geometry is not None:
                row = copy(annotation)
                row.geometry = geometry
                scoped.append(row)
        return scoped

    def canonical_annotations(self, task_id):
        identifier = uuid.UUID(str(task_id))
        if identifier not in self.sources_by_task:
            reject("video_export_scope_changed")
        return list(self._annotations_by_task[identifier])

    def video_metadata(self, task_id):
        identifier = uuid.UUID(str(task_id))
        metadata = self.metadata_by_task.get(identifier)
        if identifier not in self.sources_by_task or not isinstance(metadata, dict):
            reject("video_export_metadata_unavailable")
        return deepcopy(metadata)

    def service(self, db, *, media_paths=None):
        return FrozenVideoExportService(db, self, media_paths=media_paths)


class FrozenVideoExportService(image_frozen.FrozenExportService):
    trusted_video_metadata = True

    def _scope(self, project_id, batch_id):
        if project_id != self.frozen.project_id or batch_id != self.frozen.batch_id:
            reject()

    def _video_scope(self, scope):
        expected = self.frozen.source_scope
        if (scope.as_dict() if scope else None) != (
            expected.as_dict() if expected else None
        ):
            reject("video_export_scope_changed")

    async def _load_data(self, project_id, batch_id=None, *, video_scope=None):
        self._scope(project_id, batch_id)
        self._video_scope(video_scope)
        return self.frozen.project, self.frozen.tasks, self.frozen._filtered_annotations

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
        self._video_scope(video_scope)
        if type(chunk_size) is not int or not 0 < chunk_size <= 1000:
            reject()
        for start in range(0, len(self.frozen.tasks), chunk_size):
            tasks = self.frozen.tasks[start : start + chunk_size]
            annotations = (
                {task.id: self.frozen.canonical_annotations(task.id) for task in tasks}
                if with_annotations
                else {}
            )
            yield tasks, annotations, await self._load_dataset_items(tasks)

    def video_metadata(self, task, item):
        return self.frozen.video_metadata(task.id)

    def video_source(self, project, task, item, metadata, scope=None):
        self._video_scope(scope)
        source = self.frozen.sources_by_task.get(task.id)
        receipt = self.frozen.metadata_receipts.get(task.id)
        if (
            source is None
            or not isinstance(receipt, dict)
            or metadata != self.frozen.video_metadata(task.id)
        ):
            reject()
        count = metadata.get("frame_count")
        if type(count) is not int or not 0 < count <= 1_000_000:
            reject("video_export_metadata_invalid")
        contract = source_contract(project)
        step = derive_step(metadata.get("fps"), contract["video_sampling"])
        frames = derive_sampled_frames(count, step)
        if scope is not None:
            frames = scope.filter_frames(frames)
        result = {
            "task_id": str(task.id),
            "dataset_item_id": source.get("item_id"),
            "source_key": source["object_key"],
            "content_hash": source["sha256"],
            "source_sha256": source["sha256"],
            "source_size_bytes": source["size_bytes"],
            "probe_contract": 1,
            "metadata_receipt": deepcopy(receipt),
            "sampling": contract["video_sampling"],
            "collaboration": contract["video_collaboration"],
            "frame_index_base": 0,
            "grid_origin": 0,
            "resolved_step": step,
            "source_frame_indices": frames,
            "source_sample_ids": [f"{task.id}:{frame}" for frame in frames],
            "annotation_resolver": "canonical-video-tracks-1",
        }
        if scope is not None:
            result["export_scope"] = scope.as_dict()
        return result
