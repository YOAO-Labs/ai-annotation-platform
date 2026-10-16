"""Task/media/format metadata shared by all annotation export entrypoints."""

from copy import deepcopy

from aap_task_contract import TASK_VALUES

from app.services.video_context import derive_sampled_frames, derive_step

EXPORT_CONTRACT_REVISION = 1
TOOL_TASKS = {
    "bbox": "detect",
    "region": "segment",
    "rotated_bbox": "obb",
    "keypoint": "pose",
    "lidar_box_3d": "detect",
    "point_mask_3d": "segment",
}
TARGETS = {
    "yolo": ("detect", "image", "ultralytics-yolo"),
    "yolo-det": ("detect", "image", "ultralytics-yolo"),
    "yolo-seg": ("segment", "image", "ultralytics-yolo"),
    "yolo-obb": ("obb", "image", "ultralytics-yolo"),
    "yolo-frames-det": ("detect", "image", "ultralytics-yolo"),
    "yolo-frames-seg": ("segment", "image", "ultralytics-yolo"),
    "coco-frames-seg": ("segment", "image", "coco"),
    "coco-multicamera": ("detect", "image", "coco"),
    "voc": ("detect", "image", "pascal-voc"),
    "mot": ("detect", "image", "mot"),
    "davis": ("segment", "image", "davis"),
    "youtube-vos": ("segment", "image", "youtube-vos"),
    "mots": ("segment", "image", "mots"),
    "binary-png": ("segment", "image", "binary-png"),
    "indexed-png": ("segment", "image", "indexed-png"),
    "label-studio-brush": ("segment", "image", "label-studio-brush"),
    "pointmask": ("segment", "lidar", "pointmask"),
    "nuscenes": ("detect", "lidar", "nuscenes"),
}


def tasks_for_tools(tool_ids):
    found = {TOOL_TASKS[key] for key in tool_ids if key in TOOL_TASKS}
    return [task for task in TASK_VALUES if task in found]


def project_tasks(project):
    return tasks_for_tools(
        key
        for key, binding in (project.tool_bindings or {}).items()
        if binding.get("enabled")
    )


def source_contract(project):
    media_type = project.data_type or "image"
    result = {
        "project_id": str(project.id),
        "media_type": media_type,
        "tool_bindings": deepcopy(project.tool_bindings or {}),
    }
    if media_type == "video":
        result.update(
            video_sampling=deepcopy(getattr(project, "video_sampling", None) or {}),
            video_collaboration=deepcopy(
                getattr(project, "video_collaboration", None) or {}
            ),
        )
    return result


def target_contract(project, target):
    available = project_tasks(project)
    unmapped = [
        key
        for key, binding in (project.tool_bindings or {}).items()
        if binding.get("enabled") and key not in TOOL_TASKS
    ]
    if target in TARGETS:
        task, media_type, format_name = TARGETS[target]
    elif target == "kitti":
        task, media_type, format_name = (
            "detect",
            "lidar" if project.data_type == "lidar" else "image",
            "kitti",
        )
    elif target in {"aap_json", "video_json", "coco"}:
        task = available[0] if len(available) == 1 and not unmapped else None
        media_type = project.data_type or "image"
        format_name = {
            "aap_json": "aap",
            "video_json": "aap-video-tracks",
            "coco": "coco",
        }[target]
    else:
        raise ValueError(f"Unknown export target: {target}")
    return {
        "target": target,
        "task": task,
        "available_tasks": [task] if task else available,
        "media_type": media_type,
        "format": format_name,
        "purpose": "annotation-export",
        "training_qualified": False,
        "unmapped_tool_units": unmapped
        if target in {"aap_json", "video_json", "coco"}
        else [],
    }


def export_contracts(project, targets):
    return {
        "schema": EXPORT_CONTRACT_REVISION,
        "contracts": [target_contract(project, target) for target in targets],
        "source": source_contract(project),
    }


def video_source_contract(project, task, item, metadata, scope=None):
    """Freeze the absolute source grid shared by JSON and archive exports."""
    from app.services.video_frame_service import source_key_for_item

    source = source_contract(project)
    step = derive_step(metadata.get("fps"), source["video_sampling"])
    frames = derive_sampled_frames(metadata.get("frame_count") or 0, step)
    if scope is not None:
        frames = scope.filter_frames(frames)
    result = {
        "task_id": str(task.id),
        "dataset_item_id": str(item.id) if item else None,
        "source_key": source_key_for_item(item) if item else task.file_path,
        "content_hash": item.content_hash if item else None,
        "sampling": source["video_sampling"],
        "collaboration": source["video_collaboration"],
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
