"""Pure archive naming contracts shared by input admission and packaging."""

from copy import deepcopy
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting import image_media_paths as paths
from tests.factory import build_tool_bindings


def snapshot(*, targets=None, names=("same.jpg", "same.png"), reference=False):
    tasks = [
        {
            "id": str(uuid.uuid4()),
            "file_name": name,
            "display_id": f"T-{index}",
            "dataset_item_id": str(uuid.uuid4()),
        }
        for index, name in enumerate(names)
    ]
    result = {
        "project": {"tool_bindings": build_tool_bindings(["car"])},
        "tasks": tasks,
        "targets": targets or ["coco"],
        "options": {},
        "annotations": [],
        "media": [],
        "references": {},
    }
    for task in tasks:
        if reference:
            result["references"][task["id"]] = {"dataset_id": str(uuid.uuid4())}
        else:
            result["media"].append(
                {
                    "task_id": task["id"],
                    "role": "image",
                    "link_id": None,
                    "source_key": "a" * 64,
                }
            )
    return result


def test_shared_original_has_distinct_task_paths_and_parallel_yolo_destinations():
    data = snapshot(targets=["coco", "yolo", "yolo-det", "aap_json"])
    relative = paths.paths(data)
    assert len(set(relative.values())) == 2
    assert all(
        path.startswith(f"tasks/{identifier}/") for identifier, path in relative.items()
    )
    mappings = paths.logical_sources(data)
    assert set(mappings) == set(data["targets"])
    assert all(set(mapping.values()) == {"a" * 64} for mapping in mappings.values())
    for identifier, destinations in paths.archive_paths(data).items():
        assert set(destinations) == {
            f"images/{relative[identifier]}",
            f"yolo/images/{relative[identifier]}",
            f"yolo-det/images/{relative[identifier]}",
        }


def test_reference_paths_preserve_the_existing_dataset_item_task_contract():
    data = snapshot(names=("dir\\hello world.JPEG",), reference=True)
    task = data["tasks"][0]
    reference = data["references"][task["id"]]
    assert (
        paths.paths(data)[uuid.UUID(task["id"])]
        == f"{reference['dataset_id']}/{task['dataset_item_id']}/{task['id']}/hello_world.jpeg"
    )
    assert paths.logical_sources(data) == {"coco": {}}


@pytest.mark.parametrize("targets", [["coco"], sorted(paths.TARGETS)])
def test_empty_scope_has_all_targets_without_copy_sources(targets):
    data = snapshot(targets=targets, names=())
    assert paths.paths(data) == {}
    assert paths.archive_paths(data) == {}
    assert paths.logical_sources(data) == {target: {} for target in targets}


def test_indexed_png_generated_name_collision_is_rejected_before_packaging():
    data = snapshot(targets=["indexed-png"])
    for task in data["tasks"]:
        task["display_id"] = "same-display"
        data["annotations"].append(
            {
                "id": str(uuid.uuid4()),
                "task_id": task["id"],
                "class_name": "car",
                "geometry": {"type": "raster_mask"},
            }
        )
    with pytest.raises(ContentError, match="archive_path_conflict"):
        paths.logical_sources(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_task",
        "missing_managed",
        "reference_and_managed",
        "unknown_binding",
        "traversal_mask",
        "unsafe_target",
    ],
)
def test_invalid_scope_or_generated_paths_are_rejected(mutation):
    data = snapshot(targets=["binary-png"])
    task = data["tasks"][0]
    if mutation == "duplicate_task":
        data["tasks"].append(deepcopy(task))
    elif mutation == "missing_managed":
        data["media"].pop()
    elif mutation == "reference_and_managed":
        data["references"][task["id"]] = {"dataset_id": str(uuid.uuid4())}
    elif mutation == "unknown_binding":
        data["media"][0]["task_id"] = str(uuid.uuid4())
    elif mutation == "traversal_mask":
        task["display_id"] = "../root"
        data["annotations"].append(
            {
                "id": str(uuid.uuid4()),
                "task_id": task["id"],
                "class_name": "car",
                "geometry": {"type": "raster_mask"},
            }
        )
    else:
        data["targets"] = ["../coco"]
    with pytest.raises(ContentError):
        paths.logical_sources(data)
