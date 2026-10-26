"""Reproduce LiDAR delivery paths from the exact frozen media bindings."""

from __future__ import annotations

import os
from types import SimpleNamespace


def logical_sources(
    snapshot: dict, *, allow_missing_kitti_camera: bool = False
) -> dict[str, dict[str, str]]:
    """Map target-relative paths to canonical source keys without physical I/O.

    ``allow_missing_kitti_camera`` is preflight-only: until the user selects a
    KITTI camera, the frozen scope deliberately has no camera binding, so path
    planning must still validate the primary frames that the report uses.
    """
    # Packaging owns the persisted naming conventions. Import at use time so
    # the collector and packaging can both consume this helper without a cycle.
    from app.services.exporting.packaging import (
        _camera_name,
        _lidar_frame_key,
        _lidar_media_path,
    )

    targets = [
        target
        for target in snapshot["targets"]
        if target in {"kitti", "pointmask", "coco-multicamera", "nuscenes"}
    ]
    result: dict[str, dict[str, str]] = {target: {} for target in targets}
    generated = {target: set() for target in targets}
    items = {item["id"]: SimpleNamespace(**item) for item in snapshot["items"]}
    links = {link["id"]: SimpleNamespace(**link) for link in snapshot["links"]}
    scenes = {scene["id"]: scene for scene in snapshot.get("scenes", [])}
    cameras_by_task = {}
    for link in links.values():
        if link.role.startswith("camera_"):
            cameras_by_task.setdefault(link.task_id, []).append(link)
    selected = (snapshot.get("options", {}).get("lidar") or {}).get("kitti_camera_role")
    bindings = {}
    for binding in snapshot["media"]:
        key = (binding["task_id"], binding["role"])
        if key in bindings:
            raise ValueError("lidar_media_binding_conflict")
        bindings[key] = binding

    def add(target, path, binding):
        if (
            not isinstance(path, str)
            or not path
            or len(path.encode("utf-8")) > 4096
            or any(char in path for char in ("\\", ":"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise ValueError("lidar_media_path_invalid")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("lidar_media_path_invalid")
        if (
            target == "nuscenes"
            and snapshot.get("schema") == 2
            and binding["role"] != "primary_lidar"
        ):
            if path.split("/")[0] in {
                "README.txt",
                "media_manifest.json",
                "fetch_nuscenes_media.py",
                "v1.0-aap",
                "support",
            }:
                raise ValueError("lidar_media_path_collision")
        source_key = binding["source_key"]
        previous = result[target].setdefault(path, source_key)
        if previous != source_key:
            raise ValueError("lidar_media_path_collision")

    def generated_path(target, path):
        if path in generated[target]:
            raise ValueError("lidar_media_path_collision")
        generated[target].add(path)

    def binding_for(task_id, role):
        try:
            return bindings[(task_id, role)]
        except KeyError:
            raise ValueError("lidar_media_binding_missing") from None

    for task in snapshot["tasks"]:
        task_id = task["id"]
        camera_links = cameras_by_task.get(task_id, [])
        for target in targets:
            if target == "nuscenes" and snapshot.get("schema") == 2:
                primary = binding_for(task_id, "primary_lidar")
                primary_item = items.get(primary["source"].get("item_id"))
                if primary_item is None:
                    raise ValueError("lidar_media_item_missing")
                add(
                    target,
                    f"support/pointclouds/{task_id}/{_lidar_media_path(primary_item)}",
                    primary,
                )
                raw = binding_for(task_id, "raw_lidar")
                add(
                    target,
                    primary_item.metadata_["nuscenes_export"]["sample_data"][
                        "filename"
                    ],
                    raw,
                )
                scene = scenes.get(str(primary_item.scene_id))
                if scene is None:
                    raise ValueError("lidar_media_scene_missing")
                add(
                    target,
                    scene["source_metadata"]["nuscenes_export"]["map"]["filename"],
                    binding_for(task_id, "map"),
                )
                for link in camera_links:
                    binding = binding_for(task_id, link.role)
                    if binding["link_id"] != link.id:
                        raise ValueError("lidar_media_binding_conflict")
                    item = items.get(link.dataset_item_id)
                    if item is None or binding["source"].get("item_id") != item.id:
                        raise ValueError("lidar_media_item_missing")
                    add(
                        target,
                        item.metadata_["nuscenes_export"]["sample_data"]["filename"],
                        binding,
                    )
                continue
            if target in {"kitti", "pointmask"}:
                primary = binding_for(task_id, "primary_lidar")
                item = items.get(primary["source"].get("item_id"))
                frame = _lidar_frame_key(SimpleNamespace(**task), item)
                generated_path(
                    target,
                    f"label_2/{frame}.txt"
                    if target == "kitti"
                    else f"segmentation/{frame}.label",
                )
                if target == "kitti":
                    generated_path(target, f"calib/{frame}.txt")
            if target in {"kitti", "pointmask", "nuscenes"}:
                primary = binding_for(task_id, "primary_lidar")
                item_id = primary["source"].get("item_id")
                if item_id is None:
                    relative = os.path.basename(task["file_path"])
                else:
                    if item_id not in items:
                        raise ValueError("lidar_media_item_missing")
                    relative = _lidar_media_path(items[item_id])
                path = (
                    f"support/pointclouds/{task_id}/{relative}"
                    if target == "nuscenes"
                    else f"velodyne/{relative}"
                )
                add(target, path, primary)
                if target == "nuscenes":
                    continue
            cameras = camera_links
            if target == "kitti":
                cameras = [link for link in cameras if link.role == selected]
                if not cameras and not (
                    allow_missing_kitti_camera and selected is None
                ):
                    raise ValueError("lidar_media_camera_missing")
            for link in cameras:
                binding = binding_for(task_id, link.role)
                if binding["link_id"] != link.id:
                    raise ValueError("lidar_media_binding_conflict")
                item_id = link.dataset_item_id
                if item_id not in items or binding["source"].get("item_id") != item_id:
                    raise ValueError("lidar_media_item_missing")
                item = items[item_id]
                if target == "coco-multicamera":
                    relative = f"images/{link.role}/{task_id}/{os.path.basename(item.file_path)}"
                else:
                    cam = _camera_name(link)
                    relative = f"images/{cam}/{_lidar_media_path(item)}"
                    generated_path(target, f"calib_raw/{cam}/{frame}.json")
                add(target, relative, binding)
    return result
