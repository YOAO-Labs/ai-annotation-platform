"""Enumerated native model identities; discovery is separate from qualification."""

from dataclasses import dataclass

from .dataset import DETECTION, SEGMENTATION

ULTRALYTICS_VERSION = "8.4.183"
LEGACY_ULTRALYTICS_VERSION = "8.4.177"
LIBREYOLO_VERSION = "1.6.0"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    framework: str
    family: str
    scale: str
    task: str
    head: str
    end2end: bool = False

    @property
    def yaml_filename(self):
        return self.id + ".yaml"


MODEL_SPECS = tuple(
    ModelSpec(
        f"{family}{scale}{'-seg' if task == SEGMENTATION else ''}",
        "ultralytics",
        family,
        scale,
        task,
        ("Segment26" if family == "yolo26" else "Segment")
        if task == SEGMENTATION
        else "Detect",
        family == "yolo26",
    )
    for family in ("yolov8", "yolo11", "yolo26")
    for task in (DETECTION, SEGMENTATION)
    for scale in "nsmlx"
) + tuple(
    ModelSpec(f"libreyolo9{scale}", "libreyolo", "yolo9", scale, DETECTION, "YOLO9")
    for scale in "tsmc"
)
_MODELS = {(item.framework, item.id): item for item in MODEL_SPECS}
LEGACY_ULTRALYTICS_MODELS = frozenset(
    {f"yolo11{scale}" for scale in "nsmlx"} | {"yolo11n-seg"}
)


def model_spec(framework, base_model):
    if not isinstance(base_model, str):
        raise ValueError("必须选择已登记的基础模型")
    try:
        return _MODELS[(framework, base_model.lower())]
    except KeyError:
        raise ValueError("该基础模型家族或规格没有对应的受控原生实现") from None


def models_for_framework(framework):
    return tuple(item for item in MODEL_SPECS if item.framework == framework)


def supports_version(spec, version):
    if spec.framework == "libreyolo":
        return version == LIBREYOLO_VERSION
    return version == ULTRALYTICS_VERSION or (
        version == LEGACY_ULTRALYTICS_VERSION and spec.id in LEGACY_ULTRALYTICS_MODELS
    )
