"""协议 v2 受控词表常量 (backend 侧镜像)。

SSOT 在 `apps/api/app/services/capability_registry.py`. 本文件是 backend 进程的
最小镜像 (不引入 apps/api 反向依赖), 让 backend 在 `/setup` / `/predict` 校验
入参 / 出参时直接对照常量, 避免拼字符串导致词表漂移。

protocol v2 词表稳定后改动概率低；若 capability_registry 扩展, 需同步更新
此处并 bump 本包 minor 版本 (写入 README.md)。
"""

from __future__ import annotations

from aap_task_contract import TASK_VALUES as TASK_VALUES

# `/setup.infra` / `/setup.models[].infra` 受控值. 与 capability_registry.INFRAS 同源.
INFRA_VALUES: tuple[str, ...] = (
    "pytorch",
    "onnx",
    "paddle",
    "tensorrt",
    "openvino",
    "other",
)

# `/setup.models[].supported_geometric_outputs` 受控值. 与 capability_registry.GEOMETRIES 同源.
GEOMETRY_VALUES: tuple[str, ...] = (
    "bbox",
    "rotated_bbox",
    "polygon",
    "polyline",
    "keypoint",
    "lidar_box_3d",
    "point_mask_3d",
    "mask",
    "none",
)

# `/setup.supported_prompts` / `/setup.models[].supported_prompts` 受控值.
# 协议 v2 §4.1.3 定义. `none` = 纯批量, 无交互式 prompt (yolo / ocr / layout 等).
PROMPT_VALUES: tuple[str, ...] = (
    "none",
    "point",
    "interactive_box",
    "text",
    "exemplar",
    "scribble",
    "sketch",
    "mask",
    "correction_frame",
    "bbox",
)

# `/setup.models[].supported_inputs` 受控值. `video` 只能由 backend 显式声明, 平台不合成。
INPUT_VALUES: tuple[str, ...] = (
    "full_image",
    "crop",
    "bbox_prompt",
    "point_prompt",
    "mask_prompt",
    "scribble_prompt",
    "video",
)

# task → 默认几何输出 (capability_registry.TASK_DEFAULT_GEOMETRY 镜像).
# backend 没显式声明 `supported_geometric_outputs` 时, apps/api 会合成此默认。
TASK_DEFAULT_GEOMETRY: dict[str, tuple[str, ...]] = {
    "detect": ("bbox",),
    "obb": ("rotated_bbox",),
    "segment": ("polygon",),
    "pose": ("keypoint",),
    "classify": ("none",),
    "ocr": ("bbox",),
    "doc_layout": ("bbox",),
    "tracker": (),
    "interactive_seg": ("polygon",),
}
