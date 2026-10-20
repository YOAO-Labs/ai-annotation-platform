"""Pretrained class declarations for the stable setup capability contract.

These tables are unverified manifests. Concrete detection weights are inspected
through model_metadata after loading; their actual names drive inference and
class-filter validation. Open-vocabulary classes remain request-defined.
"""

from __future__ import annotations

# COCO 80 类 (ultralytics 官方顺序, detection / segmentation / 大多数预训练权重)。
COCO80: list[str] = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
]

# DOTA-v1.0 15 类 (obb 预训练权重)。
DOTA15: list[str] = [
    "plane",
    "ship",
    "storage tank",
    "baseball diamond",
    "tennis court",
    "basketball court",
    "ground track field",
    "harbor",
    "bridge",
    "large vehicle",
    "small vehicle",
    "helicopter",
    "roundabout",
    "soccer ball field",
    "swimming pool",
]

# YOLO pose 单类。
COCO_POSE: list[str] = ["person"]


def classes_for_task(task: str) -> list[dict] | None:
    """按 task 返回静态类别表 [{"index": i, "name": n}, ...];无静态表的 task 返回 None。

    detection / segmentation → COCO80, obb → DOTA15, keypoint → COCO_POSE,
    其它 (含开集 task) → None。
    """
    from aap_task_contract import normalize_task

    task = normalize_task(task, allow_unknown=True)
    if task in ("detect", "segment"):
        names = COCO80
    elif task == "obb":
        names = DOTA15
    elif task == "pose":
        names = COCO_POSE
    else:
        return None
    return [{"index": i, "name": n} for i, n in enumerate(names)]
