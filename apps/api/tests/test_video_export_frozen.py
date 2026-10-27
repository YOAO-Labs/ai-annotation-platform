"""Independent pure video replay tests; unittest never loads DB autouse fixtures."""

from copy import deepcopy
import unittest
from unittest.mock import patch
import uuid

from app.services.content_snapshot import ContentError
from app.services.exporting.video_frozen import FrozenVideo, scope_from_dict
from app.services.exporting import video_media_paths as paths
from app.services.mask_formats.contracts import canonical_digest
from app.services.video_canonical import VideoBoundaryUnreconciledError
from app.services.video_track_quality import _snapshot
from app.services.exporting.video_mask_formats import exact_mask_frames


def facts(*, original=False, collaboration=False):
    project, task, item, dataset = [str(uuid.uuid4()) for _ in range(4)]
    stamp = "2026-01-01T00:00:00+00:00"
    annotation = {
        "id": str(uuid.uuid4()),
        "project_id": project,
        "task_id": task,
        "class_name": "car",
        "tool_unit_id": "bbox",
        "annotation_type": "video_track_bbox",
        "geometry": {
            "type": "video_track_bbox",
            "track_id": "trk_raw",
            "keyframes": [
                {"frame_index": 0, "bbox": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}},
                {"frame_index": 9, "bbox": {"x": 0.5, "y": 0, "w": 0.2, "h": 0.2}},
            ],
            "outside": [],
        },
        "source": "manual",
        "attributes": {"color": "red", "removed": "private"},
        "is_active": True,
        "was_cancelled": False,
        "version": 1,
        "track_id": "trk_raw",
        "created_at": stamp,
    }
    return {
        "schema": 4,
        "probe_contract": 1,
        "project_id": project,
        "batch_id": None,
        "task_ids": None,
        "targets": ["video_json", "aap_json"],
        "options": {},
        "project": {
            "id": project,
            "display_id": "P1",
            "name": "video",
            "type_key": "video-track",
            "data_type": "video",
            "video_sampling": {"mode": "step", "frame_step": 3},
            "video_collaboration": {
                "enabled": collaboration,
                "overlap_frames": 2 if collaboration else 0,
            },
            "tool_bindings": {
                "bbox": {
                    "enabled": True,
                    "classes": [{"name": "car"}],
                    "attribute_schema": {"fields": [{"key": "color", "type": "text"}]},
                }
            },
        },
        "tasks": [
            {
                "id": task,
                "project_id": project,
                "dataset_item_id": None if original else item,
                "original_id": str(uuid.uuid4()) if original else None,
                "display_id": "T1",
                "file_name": "clip.mp4",
                "file_path": "saved/original",
                "file_type": "video",
                "version": 1,
                "created_at": stamp,
            }
        ],
        "batch": None,
        "links": [],
        "items": []
        if original
        else [
            {
                "id": item,
                "dataset_id": dataset,
                "file_path": "saved/original",
                "file_type": "video",
                "width": 999,
                "height": 888,
                "metadata_": {"video": {"frame_count": 999999}},
            }
        ],
        "datasets": []
        if original
        else [{"id": dataset, "name": "dataset", "metadata_": {}}],
        "annotations": [annotation],
        "predictions": [],
        "scenes": [],
        "tracks": [],
        "intervals": [],
        "segments": [],
        "quality_runs": [],
        "references": {},
        "media": [
            {
                "task_id": task,
                "role": "video",
                "link_id": None,
                "source_key": "a" * 64,
                "source": {
                    "owner_kind": "task_original" if original else "dataset_asset",
                    "item_id": None if original else item,
                    "object_key": "saved/original",
                    "sha256": "b" * 64,
                    "size_bytes": 7,
                    "media_type": "video",
                },
            }
        ],
    }


def collaborative():
    value = facts(collaboration=True)
    task, item = value["tasks"][0]["id"], value["items"][0]["id"]
    left, right = [str(uuid.uuid4()) for _ in range(2)]
    value["segments"] = [
        {
            "id": left,
            "dataset_item_id": item,
            "segment_index": 0,
            "start_frame": 0,
            "end_frame": 4,
        },
        {
            "id": right,
            "dataset_item_id": item,
            "segment_index": 1,
            "start_frame": 5,
            "end_frame": 9,
        },
    ]
    first = value["annotations"][0]
    first["video_segment_id"] = left
    first["track_id"] = "trk_b"
    first["geometry"]["keyframes"][1]["frame_index"] = 4
    second = deepcopy(first)
    second.update(id=str(uuid.uuid4()), video_segment_id=right, track_id="trk_a")
    second["geometry"]["keyframes"][0]["frame_index"] = 5
    second["geometry"]["keyframes"][1]["frame_index"] = 9
    value["annotations"].append(second)
    frames = [3, 6]
    snapshot = [
        {
            "annotation_id": row["id"],
            "segment_id": row["video_segment_id"],
            "version": row["version"],
            "geometry_digest": canonical_digest(row["geometry"]),
            "class_name": row["class_name"],
            "tool_unit_id": row["tool_unit_id"],
            "track_id": row["track_id"],
            "geometry_family": "bbox",
        }
        for row in sorted(
            value["annotations"], key=lambda row: (row["video_segment_id"], row["id"])
        )
    ]
    run = {
        "id": "00000000-0000-0000-0000-000000000001",
        "task_id": task,
        "left_segment_id": left,
        "right_segment_id": right,
        "status": "accepted",
        "created_at": "2026-01-01T00:00:00+00:00",
        "sampling_digest": canonical_digest(frames),
        "input_digest": canonical_digest(
            {"annotations": snapshot, "sampling_digest": canonical_digest(frames)}
        ),
        "metrics": {"sampling_frames": frames},
        "pairs": [
            {
                "left_annotation_id": first["id"],
                "right_annotation_id": second["id"],
                "decision": "same_track",
            }
        ],
    }
    value["quality_runs"] = [run]
    return value


class FrozenVideoTests(unittest.IsolatedAsyncioTestCase):
    def test_task_original_has_no_fabricated_item_and_uses_probe_metadata(self):
        value = facts(original=True)
        frozen = FrozenVideo(value)
        identifier = uuid.UUID(value["tasks"][0]["id"])
        self.assertIsNone(frozen.items_by_task[identifier])
        self.assertEqual(frozen.items, {})
        with self.assertRaisesRegex(ContentError, "metadata_unavailable"):
            frozen.video_metadata(identifier)
        metadata = {"fps": 30, "frame_count": 10, "width": 4, "height": 3}
        frozen.metadata_by_task[identifier] = metadata
        frozen.metadata_receipts[identifier] = {
            "probe_contract": 1,
            "video": metadata,
            "source_sha256": "b" * 64,
            "source_size_bytes": 7,
        }
        result = frozen.service(object()).video_source(
            frozen.project, frozen.tasks[0], None, metadata
        )
        self.assertEqual(result["source_frame_indices"], [0, 3, 6, 9])
        self.assertIsNone(result["dataset_item_id"])
        self.assertEqual(result["source_key"], "saved/original")
        self.assertEqual(result["content_hash"], "b" * 64)
        self.assertEqual(
            result["metadata_receipt"], frozen.metadata_receipts[identifier]
        )
        result["metadata_receipt"]["video"]["fps"] = 999
        self.assertEqual(frozen.video_metadata(identifier)["fps"], 30)

    def test_scope_uses_absolute_grid_and_boundary_keyframes(self):
        value = facts(original=True)
        identifier = uuid.UUID(value["tasks"][0]["id"])
        value["options"]["video_export_scope"] = {
            "task_id": str(identifier),
            "dataset_item_id": None,
            "selection_kind": "frames",
            "from_frame": 4,
            "to_frame": 8,
        }
        frozen = FrozenVideo(value)
        geometry = frozen.canonical_annotations(identifier)[0].geometry
        self.assertEqual([row["frame_index"] for row in geometry["keyframes"]], [4, 8])
        metadata = {"fps": 30, "frame_count": 10, "width": 4, "height": 3}
        frozen.metadata_by_task[identifier] = metadata
        frozen.metadata_receipts[identifier] = {"probe_contract": 1, "video": metadata}
        source = frozen.service(object()).video_source(
            frozen.project, frozen.tasks[0], None, metadata, frozen.source_scope
        )
        self.assertEqual(source["source_frame_indices"], [6])
        self.assertEqual(source["source_sample_ids"], [f"{identifier}:6"])

    def test_fresh_accepted_falls_back_without_mutating_newer_stale_candidate(self):
        value = collaborative()
        old = value["quality_runs"][0]
        newest = {
            **deepcopy(old),
            "id": "00000000-0000-0000-0000-000000000002",
            "input_digest": "0" * 64,
        }
        value["quality_runs"].insert(0, newest)
        before = deepcopy(value)
        frozen = FrozenVideo(value)
        task = uuid.UUID(value["tasks"][0]["id"])
        rows = frozen.canonical_annotations(task)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].track_id, "trk_a")
        self.assertEqual(
            [row["frame_index"] for row in rows[0].geometry["keyframes"]], [0, 4, 5, 9]
        )
        self.assertEqual(frozen.selected_quality_runs[task], (uuid.UUID(old["id"]),))
        self.assertEqual(value, before)
        self.assertTrue(all(run.status == "accepted" for run in frozen.quality_runs))

    def test_same_timestamp_uses_uuid_order(self):
        value = collaborative()
        old = value["quality_runs"][0]
        newest = {
            **deepcopy(old),
            "id": "00000000-0000-0000-0000-000000000002",
            "pairs": [],
        }
        value["quality_runs"] = [old, newest]
        frozen = FrozenVideo(value)
        task = uuid.UUID(value["tasks"][0]["id"])
        self.assertEqual(len(frozen.canonical_annotations(task)), 2)
        self.assertEqual(frozen.selected_quality_runs[task], (uuid.UUID(newest["id"]),))

    def test_quality_candidates_hash_each_raw_geometry_only_once(self):
        value = collaborative()
        candidate = deepcopy(value["quality_runs"][0])
        candidate.update(
            id="00000000-0000-0000-0000-000000000002", input_digest="0" * 64
        )
        candidate["metrics"]["sampling_frames"] = [0, 3, 6, 9]
        value["quality_runs"].append(candidate)
        with patch(
            "app.services.exporting.video_frozen._snapshot", wraps=_snapshot
        ) as snapshot:
            frozen = FrozenVideo(value)
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(len(frozen.canonical_annotations(frozen.tasks[0].id)), 1)

    def test_quality_digest_uses_raw_filtered_class_and_version(self):
        value = collaborative()
        hidden = deepcopy(value["annotations"][0])
        hidden.update(id=str(uuid.uuid4()), class_name="removed")
        value["annotations"].append(hidden)
        with self.assertRaises(VideoBoundaryUnreconciledError) as caught:
            FrozenVideo(value)
        self.assertEqual(caught.exception.detail["boundaries"][0]["status"], "stale")

    def test_scope_not_crossing_boundary_needs_no_quality_acceptance(self):
        value = collaborative()
        value["quality_runs"] = []
        value["options"]["video_export_scope"] = {
            "task_id": value["tasks"][0]["id"],
            "dataset_item_id": value["items"][0]["id"],
            "selection_kind": "frames",
            "from_frame": 1,
            "to_frame": 3,
        }
        frozen = FrozenVideo(value)
        self.assertEqual(len(frozen.canonical_annotations(frozen.tasks[0].id)), 1)

    def test_mask_scope_retains_exact_boundary_frames_for_youtube(self):
        value = facts(original=True)
        value["annotations"][0]["geometry"] = {
            "type": "video_track_mask",
            "keyframes": [
                {"frame_index": 0, "mask": {"sha256": "a" * 64}},
                {"frame_index": 9, "mask": {"sha256": "b" * 64}},
            ],
            "outside": [],
        }
        value["options"]["video_export_scope"] = {
            "task_id": value["tasks"][0]["id"],
            "dataset_item_id": None,
            "selection_kind": "frames",
            "from_frame": 4,
            "to_frame": 8,
        }
        frozen = FrozenVideo(value)
        self.assertEqual(
            exact_mask_frames(
                frozen.canonical_annotations(frozen.tasks[0].id)[0].geometry
            ),
            [4, 8],
        )

    async def test_detached_service_never_queries_and_keeps_aap_prediction_scope(self):
        value = facts(original=True)
        frozen = FrozenVideo(value)

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError("live DB read: " + name)

        service = frozen.service(Forbidden())
        project, tasks, annotations = await service._load_data(frozen.project_id)
        self.assertEqual(project.id, frozen.project_id)
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0].attributes, {"color": "red"})
        chunks = [row async for row in service.iter_export_chunks(frozen.project_id)]
        self.assertEqual(chunks[0][2], {})
        self.assertEqual(chunks[0][0][0].id, tasks[0].id)
        self.assertEqual(
            await service._load_predictions(frozen.project_id, [tasks[0].id]), []
        )

    def test_unique_safe_paths_and_exact_managed_coverage(self):
        value = facts()
        other = deepcopy(value["tasks"][0])
        other["id"] = str(uuid.uuid4())
        value["tasks"].append(other)
        value["media"].append({**deepcopy(value["media"][0]), "task_id": other["id"]})
        self.assertEqual(len(set(paths.paths(value).values())), 2)
        self.assertEqual(len(set(paths.sequence_paths(value).values())), 2)
        planned = paths.logical_sources(value)
        self.assertEqual(len(planned["video_json"]), 2)
        value["media"].pop()
        with self.assertRaises(ContentError):
            paths.logical_sources(value)

    def test_scope_rejects_bool_and_stale_segment_identity(self):
        value = collaborative()
        with self.assertRaises(ContentError):
            scope_from_dict(
                {
                    "task_id": value["tasks"][0]["id"],
                    "dataset_item_id": None,
                    "selection_kind": "frames",
                    "from_frame": False,
                    "to_frame": 5,
                }
            )
        value["options"]["video_export_scope"] = {
            "task_id": value["tasks"][0]["id"],
            "dataset_item_id": value["items"][0]["id"],
            "selection_kind": "segments",
            "from_frame": 0,
            "to_frame": 9,
            "segments": [
                {
                    key: row[key]
                    for key in ("id", "segment_index", "start_frame", "end_frame")
                }
                for row in value["segments"]
            ],
        }
        value["options"]["video_export_scope"]["segments"][0]["end_frame"] = 3
        with self.assertRaisesRegex(ContentError, "scope_changed"):
            FrozenVideo(value)


if __name__ == "__main__":
    unittest.main()
