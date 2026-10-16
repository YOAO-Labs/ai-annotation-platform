"""Canonical task names, legacy readers and media-independent task identity."""

from typing import Literal

VisualTask = Literal["detect", "segment", "classify", "pose", "obb"]
Task = Literal[
    "detect",
    "segment",
    "classify",
    "pose",
    "obb",
    "ocr",
    "doc_layout",
    "tracker",
    "interactive_seg",
]
VISUAL_TASK_VALUES = ("detect", "segment", "classify", "pose", "obb")
TASK_VALUES = (*VISUAL_TASK_VALUES, "ocr", "doc_layout", "tracker", "interactive_seg")

# These aliases are accepted at input boundaries, never emitted as new task names.
TASK_ALIASES = {
    "detection": "detect",
    "object_detection": "detect",
    "image-detection": "detect",
    "image_detection": "detect",
    "segmentation": "segment",
    "instance-segmentation": "segment",
    "instance_segmentation": "segment",
    "image-instance-segmentation": "segment",
    "image_instance_segmentation": "segment",
    "classification": "classify",
    "image-classification": "classify",
    "image_classification": "classify",
    "cls": "classify",
    "keypoint": "pose",
    "keypoints": "pose",
    "image-keypoints": "pose",
}

LEGACY_TRAINING_TASKS = {
    "detect": "image-detection",
    "segment": "image-instance-segmentation",
}
LEGACY_INFERENCE_TASKS = {
    "detect": "detection",
    "segment": "segmentation",
    "classify": "classification",
    "pose": "keypoint",
}


def normalize_task(value: str, *, allow_unknown: bool = False) -> str:
    """Resolve a known task without guessing from a model ID, format or geometry."""
    if not isinstance(value, str) or not value:
        raise ValueError("task must be a nonempty string")
    canonical = TASK_ALIASES.get(value, value)
    if canonical not in TASK_VALUES and not allow_unknown:
        raise ValueError(f"unknown task: {value}")
    return canonical


def resolve_task(task: str | None = None, task_type: str | None = None) -> str | None:
    """Merge a canonical field and its legacy alias, rejecting real conflicts."""
    current = normalize_task(task) if task is not None else None
    legacy = normalize_task(task_type) if task_type is not None else None
    if current is not None and legacy is not None and current != legacy:
        raise ValueError("task and task_type describe different visual tasks")
    return current if current is not None else legacy


def legacy_training_task(value: str) -> str:
    """Serialize only the historical training schema/request compatibility path."""
    task = normalize_task(value)
    if task not in LEGACY_TRAINING_TASKS:
        raise ValueError("task has no historical training identity")
    return LEGACY_TRAINING_TASKS[task]


def legacy_inference_task(value: str) -> str:
    """Adapt transport to external backends that still require the legacy type."""
    task = normalize_task(value)
    return LEGACY_INFERENCE_TASKS.get(task, task)


def normalize_task_fields(value):
    """Read legacy request keys while keeping prompt modes distinct from task."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    legacy = result.pop("task_type", None)
    if legacy == "text":
        if result.get("prompt_type") not in {None, "text"}:
            raise ValueError("task_type and prompt_type describe different prompts")
        result["prompt_type"] = "text"
        legacy = None
    task = resolve_task(result.get("task"), legacy)
    if task is not None:
        result["task"] = task
    return result


def normalize_modality_fields(value):
    """Read the historical reload task_type field as a media modality."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    legacy = result.pop("task_type", None)
    current = result.get("modality")
    if current is not None and legacy is not None and current != legacy:
        raise ValueError("modality and legacy task_type describe different media")
    modality = current if current is not None else legacy
    if modality is not None:
        if modality not in {"image", "video"}:
            raise ValueError("reload modality must be image or video")
        result["modality"] = modality
    return result
