"""模型类别表静态自报。

当前权重矩阵全为 ultralytics 官方预训练 (COCO / DOTA),类别表是已知真值,
按 task 静态硬编码即可,免预热、切模型稳定。index 即列表下标 (与权重 model.names 一致)。
开集 task 无固定类别,不在此处覆盖 (见 main._build_openvocab_model_entry)。
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
