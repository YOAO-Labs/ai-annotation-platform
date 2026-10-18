from __future__ import annotations

import uuid

import pytest

from aap_assets_source.frame_layout import PreparedDatasetItem
from aap_assets_source.scene_layout import (
    IngestScenePlan,
    SceneLayoutInput,
    natural_sort_key,
    plan_scenes,
)


def _item(
    path: str,
    *,
    file_type: str = "image",
    logical_path: str | None = None,
    scene_id: uuid.UUID | None = None,
) -> PreparedDatasetItem:
    return PreparedDatasetItem(
        id=uuid.uuid4(),
        file_name=path.rsplit("/", 1)[-1],
        file_path=path,
        file_type=file_type,
        logical_path=logical_path,
        scene_id=scene_id,
    )


def test_natural_sort_key_handles_numeric_frame_stems():
    assert sorted(["000010", "000001", "000002", "a"], key=natural_sort_key) == [
        "000001",
        "000002",
        "000010",
        "a",
    ]


def test_auto_role_layout_plans_one_scene_and_shared_calibration_frame_indices():
    items = (
        _item("ds/lidar/000010.pcd", file_type="point_cloud"),
        _item("ds/camera/front/000010.jpg"),
        _item("ds/lidar/000001.pcd", file_type="point_cloud"),
        _item("ds/camera/front/000001.jpg"),
        _item("ds/calib/camera/front.json", file_type="other"),
    )
    plan = plan_scenes(SceneLayoutInput("ds", items, mode="auto"))
    assert isinstance(plan, IngestScenePlan)
    assert plan.created_scenes == 1
    assert plan.assigned_items == 5
    assert plan.scenes[0].name == "ds"
    assert plan.scenes[0].source_format == "inferred"
    assert plan.scenes[0].source_metadata == {
        "mode": "auto",
        "group_key": "_single",
    }
    indexes = {
        assignment.item_id: assignment.frame_index for assignment in plan.assignments
    }
    assert indexes[items[0].id] == indexes[items[1].id] == 1
    assert indexes[items[2].id] == indexes[items[3].id] == 0
    assert indexes[items[4].id] is None
    assert any("calib items left frame_index=NULL" in note for note in plan.notes)


def test_physical_publication_paths_use_the_logical_scene_suffix():
    dataset_id = uuid.uuid4()
    item_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    prefix = f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/"
    lidar = _item(prefix + "scene_a/lidar/000001.pcd", file_type="point_cloud")
    camera = _item(
        prefix + "wrong/camera/front/000001.jpg",
        logical_path="scene_a/camera/front/000001.jpg",
    )
    plan = plan_scenes(SceneLayoutInput("dataset", (lidar, camera), mode="auto"))
    assert plan.created_scenes == 1
    assert plan.scenes[0].name == "scene_a"
    assert {assignment.frame_index for assignment in plan.assignments} == {0}


def test_legacy_name_prefix_and_per_subdirectory_layout_are_preserved():
    first = _item("legacy-ds/scene_10/frame_00010.jpg")
    second = _item("legacy-ds/scene_10/frame_00002.jpg")
    third = _item("legacy-ds/scene_2/frame_00001.jpg")
    root = _item("legacy-ds/root.jpg")
    plan = plan_scenes(
        SceneLayoutInput(
            "legacy-ds",
            (first, second, third, root),
            mode="per_subdirectory",
        )
    )
    assert [scene.name for scene in plan.scenes] == ["scene_10", "scene_2"]
    scene_by_id = {scene.id: scene.name for scene in plan.scenes}
    frame_by_item = {
        item_id: (scene_by_id[scene_id], frame_index)
        for item_id, scene_id, frame_index in (
            (assignment.item_id, assignment.scene_id, assignment.frame_index)
            for assignment in plan.assignments
        )
    }
    assert frame_by_item[first.id] == ("scene_10", 1)
    assert frame_by_item[second.id] == ("scene_10", 0)
    assert frame_by_item[third.id] == ("scene_2", 0)
    assert plan.skipped_items == 0
    assert any("1 items at dataset root" in note for note in plan.notes)


def test_single_mode_and_dry_run_return_the_same_write_plan_without_mutation():
    items = (_item("root/frame_10.jpg"), _item("root/frame_2.jpg"))
    plan = plan_scenes(SceneLayoutInput("ds", items, mode="single", dry_run=True))
    assert plan.dry_run is True
    assert plan.created_scenes == 1 and plan.assigned_items == 2
    assert len(plan.scenes) == 1 and len(plan.assignments) == 2
    assert {assignment.frame_index for assignment in plan.assignments} == {0, 1}
    assert plan.scenes[0].name == "ds"
    assert plan.notes == ("[dry-run] would create scene 'ds' with 2 items",)
    assert all(item.scene_id is None for item in items)


def test_existing_scenes_partial_state_and_root_only_behavior_remain_explicit():
    existing = plan_scenes(SceneLayoutInput("ds", (), scene_count=2, item_count=9))
    assert existing.created_scenes == 0
    assert any("2 scene(s)" in note for note in existing.notes)

    partial = plan_scenes(SceneLayoutInput("ds", (), assigned_count=1, item_count=2))
    assert partial.created_scenes == 0 and partial.skipped_items == 1
    assert any("partial migration state" in note for note in partial.notes)

    root_only = plan_scenes(
        SceneLayoutInput("ds", (_item("ds/root.jpg"),), mode="per_subdirectory")
    )
    assert root_only.created_scenes == 0 and root_only.skipped_items == 1
    assert any("root (no subdirectory) skipped" in note for note in root_only.notes)


def test_large_pseudo_scene_archive_still_hits_the_hundred_scene_guard():
    items = tuple(_item(f"ds/scene-{index}/image.jpg") for index in range(101))
    with pytest.raises(ValueError, match="inferred 101 scenes > 100"):
        plan_scenes(SceneLayoutInput("ds", items, mode="per_subdirectory"))
