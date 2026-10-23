"""v0.19.3 WS1 · 预标注编排「能力判据」纯函数 (跨保存路径 + 派发路径共用)。

把 v0.19.2 散在 trigger_preannotation 里的两条能力判据 (batchable / 分类阶段产 class)
抽成纯函数: 吃已解析的 model_caps dict (即 _stage_model 的返回, 含 resource_profile /
output_attribute_types), 不碰 backend 实体、不做 I/O, 返回 violation 列表。

- 派发路径 (trigger_preannotation): 任一 violation → 422 硬挡。
- 保存路径 (PATCH /projects/{id}): violation → 响应 capability_warnings 软提示, 不挡。

判据 SSOT 在此, 与前端 stageWarning 由共享 fixture 双端断言防漂移 (v0.19.3 WS3)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityViolation:
    """一条能力违例。code 供机器判别 (契约测试), detail 是面向用户的成句理由。"""

    code: str
    detail: str


def check_capability_violations(
    model_caps: dict,
    *,
    where: str,
    model_id,
    writes_attributes: bool,
) -> list[CapabilityViolation]:
    """对单个阶段模型校验能力判据, 返回违例列表 (空=通过)。

    判据 (均「显式自报才拦, 缺省放过」, 保持对老 backend 零退化):
    - batchable: resource_profile.batchable 显式为 False (交互/有状态) → 不能进批量预标。
    - class: 写属性的下游 (writes_attributes=True) 且模型显式自报 output_attribute_types
      但不含 'class' → 作分类下游只会产出空属性。**OCR/识别下游 (task=ocr) 豁免**: rec 产
      text/orientation/language (本就不含 class), 写回 text 即有效产出, 非「属性恒空」。

    where: 上下文前缀 ("源阶段" / "stage N ")。model_id: 仅用于成句。
    """
    violations: list[CapabilityViolation] = []
    caps = model_caps or {}

    rp = caps.get("resource_profile") or {}
    if rp.get("batchable") is False:
        violations.append(
            CapabilityViolation(
                code="not_batchable",
                detail=(
                    f"{where}模型 {model_id!r} 自报 batchable=false (交互/有状态), "
                    "不能用于批量预标注流水线"
                ),
            )
        )

    if writes_attributes:
        types = list(caps.get("output_attribute_types") or [])
        # task=ocr 是识别阶段 (产 text 非 class), 不套「分类下游须产 class」判据。
        is_ocr = caps.get("task") == "ocr"
        if types and "class" not in types and not is_ocr:
            violations.append(
                CapabilityViolation(
                    code="no_class_attribute",
                    detail=(
                        f"{where}写属性 (write.target=attributes), 但其模型 "
                        f"output_attribute_types={types} 不含 'class', 作分类下游"
                        "只会产出空属性"
                    ),
                )
            )

    return violations


# Native parents supply tight foreground bounds from verified portable RLE.
_ROI_CAPABLE_GEOMETRIES = frozenset({"bbox", "polygon", "raster_mask"})
# sam3 等老 backend 用了受控词表外的输出几何别名, 归一化后再判可裁性 (否则漏判)。
_GEO_ALIAS = {"box": "bbox", "mask": "polygon"}


def normalize_geometry_outputs(caps: dict) -> list[str]:
    """规范化模型的 supported_geometric_outputs, 兼容词表外别名 (box→bbox / mask→polygon /
    both→bbox+polygon)。非法项跳过; 缺省返回空列表。"""
    raw = (caps or {}).get("supported_geometric_outputs") or []
    out: list[str] = []
    for g in raw:
        if not isinstance(g, str):
            continue
        key = g.strip().lower()
        if key == "both":
            out.extend(("bbox", "polygon"))
        else:
            out.append(_GEO_ALIAS.get(key, key))
    return out


def check_parent_geometry_roi(
    parent_caps: dict, *, where: str, parent_model_id
) -> list[CapabilityViolation]:
    """下游阶段按上游框裁 crop / 发 bbox_prompt 作 ROI, bbox/polygon/raster_mask 可作父框
    (roi._box_bbox_pct)。校验上游父阶段的 supported_geometric_outputs 是否含可裁几何。

    与 dispatch 已有的「下游能否吃框」门 (supported_inputs 含 bbox_prompt/crop) 对称: 那道查
    下游输入侧, 这道查上游输出侧。判据「显式自报才拦, 缺省放过」——上游未自报几何 (老 backend)
    → 放过零退化; 至少能产一种可裁几何 → 放过 (可能只产 bbox 的多形态模型, 部分不可裁交由
    运行期 skipped_geometry 兜底); 完全不含可裁几何 → 违例 (该阶段所有父框运行期被跳过、零富集)。

    where: 上下文前缀 ("stage N ")。parent_model_id: 仅用于成句。
    """
    geos = normalize_geometry_outputs(parent_caps)
    if not geos:
        return []
    if any(g in _ROI_CAPABLE_GEOMETRIES for g in geos):
        return []
    return [
        CapabilityViolation(
            code="no_roi_geometry",
            detail=(
                f"{where}的上游模型 {parent_model_id!r} 只输出 {geos} (非 bbox/polygon/raster_mask), "
                "下游无法按其框裁 ROI, 该阶段所有父框将被跳过、零富集"
            ),
        )
    ]


def resolve_preannotate_queue(devices, *, gpu_queue: str, cpu_queue: str) -> str:
    """v0.19.5 · 按整条 pipeline 各阶段 resource_profile.device 决定 Celery 队列。

    保守策略: 仅当**所有阶段都显式 device=cpu** 时才进 cpu_queue; 任一阶段 device=gpu /
    未自报 (None → 视作 gpu) → 进 gpu_queue。这样老 backend / 混合 device pipeline 都落 gpu_queue,
    与现状 (全部进 ml 队列) 完全等价, 零退化。空列表 → gpu_queue。

    devices: 各阶段 device 字符串或 None 的可迭代。
    """
    normalized = [(d or "gpu").lower() for d in devices]
    if normalized and all(d == "cpu" for d in normalized):
        return cpu_queue
    return gpu_queue
