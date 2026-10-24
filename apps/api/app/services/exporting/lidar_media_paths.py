"""Reproduce LiDAR delivery paths from the exact frozen media bindings."""

from __future__ import annotations

import os
from types import SimpleNamespace


def logical_sources(snapshot: dict) -> dict[str, dict[str, str]]:
    """Map target-relative paths to canonical source keys without physical I/O."""
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
        if not path or any(char in path for char in ("\\", "\0", ":")):
            raise ValueError("lidar_media_path_invalid")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("lidar_media_path_invalid")
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
                if not cameras:
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
