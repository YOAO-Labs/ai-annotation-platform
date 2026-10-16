"""Export identities and the frozen absolute source grid."""

import uuid
from types import SimpleNamespace

from app.services.exporting.contracts import (
    export_contracts,
    target_contract,
    video_source_contract,
)
from app.services.exporting.video_scope import VideoExportScope


def project(**changes):
    return SimpleNamespace(
        id=uuid.uuid4(),
        data_type="image",
        tool_bindings={"bbox": {"enabled": True}},
        **changes,
    )


def test_multi_target_and_unmapped_native_annotations_never_claim_one_task():
    source = project()
    contract = export_contracts(source, ["yolo-det", "yolo-seg"])
    assert [entry["task"] for entry in contract["contracts"]] == ["detect", "segment"]
    assert all(entry["format"] == "ultralytics-yolo" for entry in contract["contracts"])
    source.tool_bindings["polyline"] = {"enabled": True}
    native = target_contract(source, "aap_json")
    assert native["task"] is None and native["available_tasks"] == ["detect"]
    assert native["unmapped_tool_units"] == ["polyline"]
    assert not native["training_qualified"]


def test_video_source_grid_is_absolute_scoped_and_freezes_original_parameters():
    source = project(
        video_sampling={"mode": "fps", "target_fps": 12},
        video_collaboration={"enabled": True, "overlap_frames": 10},
    )
    source.data_type = "video"
    task = SimpleNamespace(id=uuid.uuid4(), file_path="stale-task-key")
    item = SimpleNamespace(
        id=uuid.uuid4(), file_path="original/clip.mp4", content_hash="a" * 64
    )
    scope = VideoExportScope(task.id, item.id, "frames", 3, 8)
    frozen = video_source_contract(
        source, task, item, {"fps": 30, "frame_count": 20}, scope
    )
    assert frozen["source_key"] == item.file_path
    assert frozen["resolved_step"] == 2
    assert frozen["source_frame_indices"] == [4, 6, 8]
    assert frozen["source_sample_ids"] == [f"{task.id}:{frame}" for frame in [4, 6, 8]]
    source.video_sampling["target_fps"] = 1
    source.video_collaboration["overlap_frames"] = 0
    assert (
        frozen["sampling"]["target_fps"] == 12
        and frozen["collaboration"]["overlap_frames"] == 10
    )
