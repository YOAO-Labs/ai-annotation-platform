from __future__ import annotations

from typing import Any

from app.schemas.mask_format import MaskFormatCode, MaskFormatPlan
from app.services.mask_formats.contracts import canonical_digest


_LOSS_MESSAGES = {
    "overlap_resolved": "重叠实例将按目标格式的显式 winner 规则合成。",
    "holes_polygonized": "目标格式不能保留孔洞，Mask 将转换为多边形。",
    "components_split": "一个实例的多个连通区域将在目标格式中拆分。",
    "instance_id_overflow": "目标格式的实例 ID 容量不足。",
    "overlap_policy_required": "实例存在重叠；请选择显式 winner 策略或改用逐实例格式。",
    "track_identity_lost": "目标格式不能完整保留跨帧轨迹身份。",
    "sparse_frames_collapsed": "稀疏标注帧必须映射为 outside gaps 或 nearest hold。",
    "occlusion_lost": "目标格式不能表达遮挡状态。",
    "class_id_remapped": "外部类别 ID 已通过显式映射转换。",
    "instance_id_remapped": "外部实例 ID 已通过显式映射转换。",
    "frame_base_changed": "输出帧编号基准与平台源帧不同。",
    "nonportable_media_reference": "产物包含平台对象引用，不能作为独立备份。",
    "unsupported_geometry": "目标格式不能表达该任务中的部分 geometry。",
    "unknown_label": "外部标签尚未映射到项目类别。",
    "task_not_found": "导入项无法匹配项目任务。",
    "image_size_mismatch": "外部标注尺寸与项目媒体尺寸不一致。",
    "frame_index_out_of_range": "外部帧号超出项目视频范围。",
    "not_selected": "该项没有可导入的 annotation。",
}


def _code(code: str, **detail: Any) -> MaskFormatCode:
    return MaskFormatCode(
        code=code,
        message=_LOSS_MESSAGES.get(code, code),
        detail=detail,
    )


def _worst_loss(classes: list[str]) -> str:
    if "unsupported" in classes:
        return "unsupported"
    if "lossy" in classes:
        return "lossy"
    return "lossless"


def _plan(payload: dict[str, Any]) -> MaskFormatPlan:
    plan = MaskFormatPlan.model_validate({**payload, "plan_digest": ""})
    plan.plan_digest = canonical_digest(
        plan.model_dump(mode="json", exclude={"plan_digest"})
    )
    return plan
