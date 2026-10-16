"""AAP JSON — 平台原生无损中间格式。

为「跨实例迁移 / SDK / Plugin / dataset snapshot」服务的稳定锚点。与 COCO/YOLO/VOC
并列, 但**包含**它们丢失的字段: attribute_schema 值、prediction.confidence、
annotation.source、annotation_guide、classes_config 等。

关键决策 (对照 ROADMAP §6 决策底线):
- schema_version 必备; breaking change 升 major; 1.x → 2.0 不可静默解析.
- annotations[] / predictions[] **双数组分开**, 不混 type 字段
  (CVAT 部分格式踩过坑).
- 导出严格写满 null, 导入 lenient (extra="ignore") 忽略未知字段 + 缺失按默认值.
- geometry 使用平台**内部格式** (与 annotation.geometry JSONB 对齐),
  不走 LabelStudio shape 的 {type, value: {...}} 嵌套.
- task_match oneof: display_id 优先 (全局唯一最稳); 都给则 display_id 胜出.

预测导入消费 predictions[]；标注导入消费 annotations[]。mask_objects 让
video_track_mask 的内容寻址 RLE 可跨实例迁移；scene_tracks 保留 3D
时序对象的多段存在区间与轨迹级属性。
"""

from __future__ import annotations

from aap_task_contract import Task as VisualTaskName

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── schema 常量 ─────────────────────────────────────────────────────

AAP_SCHEMA_MAJOR = 1
# v0.10.17 · 升 1.1: envelope 增加 project.tool_bindings (工具维度类别/属性绑定)
# 与 annotations/predictions 数组每条 tool_unit_id. 1.0 reader 仍能读 (extra=ignore).
# v0.10.31 · 升 1.2: task 层增加 media_type + video 子块 (视频采样/fps/帧元数据).
# 1.x reader 走 extra=ignore 容忍, media_type 缺失时默认 "image".
# v0.22.0 · 升 1.3: envelope 增 mask_objects，令 coco_rle_ref 跨实例可移植。
# 1.5: annotation entry 增 track_id / temporal_role，envelope 增 scene_tracks。
# 1.6: annotation entry 增持久化多相机成员及其标定版本关系。
AAP_SCHEMA_VERSION = "1.7"


# ── task_match (oneof) ───────────────────────────────────────────────


class AAPTaskMatch(BaseModel):
    """跨实例匹配 task 的稳定键: display_id (全局唯一) + file_path (项目内)."""

    display_id: str | None = None
    file_path: str | None = None

    model_config = ConfigDict(extra="ignore")


# ── annotations / predictions entry ──────────────────────────────────


class AAPAnnotationEntry(BaseModel):
    """导出严格写满 null；标注导入保持 lenient。"""

    geometry: dict[str, Any]
    class_name: str | None = None
    # v0.10.17 · 工具维度绑定 (1.1+). 老 1.0 reader 走 extra=ignore 容忍缺失.
    tool_unit_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    source: str | None = None  # manual / prediction_based / prediction / interpolated
    track_id: str | None = Field(default=None, max_length=64)
    temporal_role: Literal["keyframe", "derived", "sample"] | None = None
    # 多相机持久化成员：role 是跨实例稳定键，DatasetItem UUID 不进入交换格式。
    sensor_role: str | None = Field(default=None, max_length=50, pattern=r"^camera_")
    sensor_visibility: Literal["visible", "occluded", "truncated", "unknown"] | None = (
        None
    )
    calibration_revision: int | None = Field(default=None, ge=1)
    calibration_digest: str | None = Field(default=None, min_length=64, max_length=64)
    user_id: UUID | None = None
    created_at: datetime | None = None
    external_id: str | None = None  # 留 forward compat

    @model_validator(mode="after")
    def validate_sensor_member(self):
        sensor_values = (
            self.sensor_role,
            self.sensor_visibility,
            self.calibration_revision,
            self.calibration_digest,
        )
        if any(value is not None for value in sensor_values) and not all(
            value is not None for value in sensor_values
        ):
            raise ValueError("camera member sensor fields must be provided together")
        if self.sensor_role is not None and (
            self.geometry.get("type") != "bbox" or self.track_id is None
        ):
            raise ValueError("camera member requires bbox geometry and track_id")
        return self

    model_config = ConfigDict(extra="ignore")


class AAPPredictionEntry(BaseModel):
    geometry: dict[str, Any] | None = None
    # v0.10.53 · 一个 prediction entry 可合并多个 shape 写入同一 Prediction.result.
    # 与 geometry 同时存在时 shapes 优先; 老单 geometry entry 继续兼容.
    shapes: list[dict[str, Any]] | None = None
    class_name: str | None = None
    # v0.10.17 · 工具维度绑定 (1.1+).
    tool_unit_id: str | None = None
    confidence: float | None = None
    model_version: str | None = None
    score: float | None = None  # 模型整体置信度 (与 prediction.score 对齐)
    source: str | None = None  # ml_backend / external_import
    created_at: datetime | None = None
    external_id: str | None = None  # 留 forward compat

    model_config = ConfigDict(extra="ignore")


class AAPSceneTrackInterval(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    source: Literal["legacy_envelope", "manual", "imported", "derived"] = "imported"

    @model_validator(mode="after")
    def validate_frames(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("end_frame must be greater than or equal to start_frame")
        return self

    model_config = ConfigDict(extra="ignore")


class AAPSceneTrackEntry(BaseModel):
    track_id: str = Field(min_length=1, max_length=64)
    scene_name: str | None = None
    class_name: str = Field(min_length=1, max_length=100)
    presence_mode: Literal["inferred", "explicit"] = "explicit"
    attributes: dict[str, Any] = Field(default_factory=dict)
    attributes_meta: dict[str, Any] = Field(default_factory=dict)
    intervals: list[AAPSceneTrackInterval] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── task block ───────────────────────────────────────────────────────


class AAPTaskBlock(BaseModel):
    task: VisualTaskName | None = None
    available_tasks: list[VisualTaskName] = Field(default_factory=list)
    task_match: AAPTaskMatch
    file_path: str | None = None
    # v0.10.31 · 媒体维度 (1.2+). 1.x reader 走 extra=ignore, 缺失默认 "image".
    media_type: Literal["image", "video", "lidar"] = "image"
    # v0.10.31 · 视频专属元数据: 采样配置/fps/frame_count/timetable 摘要/segment/chapter.
    video: dict[str, Any] | None = None
    external_id: str | None = None
    annotations: list[AAPAnnotationEntry] = Field(default_factory=list)
    predictions: list[AAPPredictionEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── project / exported_from / envelope ──────────────────────────────


class AAPExportedFrom(BaseModel):
    platform: Literal["aap"] = "aap"
    platform_version: str | None = None
    project_display_id: str | None = None
    batch_display_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class AAPProjectMeta(BaseModel):
    name: str | None = None
    type_key: str | None = None
    # v0.10.17 · classes_config / attribute_schema 在 1.x 期间继续保留 (老 reader 派生用);
    # tool_bindings 是 1.1+ 真值字段, 1.0 reader 走 extra=ignore 容忍.
    classes_config: dict[str, Any] = Field(default_factory=dict)
    attribute_schema: dict[str, Any] = Field(default_factory=lambda: {"fields": []})
    tool_bindings: dict[str, Any] = Field(default_factory=dict)
    rendering_config: dict[str, Any] = Field(default_factory=dict)
    annotation_guide: str | None = None

    model_config = ConfigDict(extra="ignore")


class AAPJsonV1Envelope(BaseModel):
    """AAP JSON v1.0 envelope.

    导出端用 model_dump(mode="json") 严格写满 null; 导入端 model_validate()
    走 extra="ignore" 实现 lenient.
    """

    schema_version: str = AAP_SCHEMA_VERSION
    format: Literal["aap"] = "aap"
    task: VisualTaskName | None = None
    available_tasks: list[VisualTaskName] = Field(default_factory=list)
    media_type: Literal["image", "video", "lidar"] = "image"
    source: dict[str, Any] = Field(default_factory=dict)
    exported_at: datetime | None = None
    exported_from: AAPExportedFrom = Field(default_factory=AAPExportedFrom)
    project: AAPProjectMeta = Field(default_factory=AAPProjectMeta)
    mask_objects: dict[str, dict[str, Any]] = Field(default_factory=dict)
    scene_tracks: list[AAPSceneTrackEntry] = Field(default_factory=list)
    tasks: list[AAPTaskBlock] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── ImportResult (predictions/import 端点响应) ──────────────────────


class AAPImportErrorEntry(BaseModel):
    task_match: dict[str, Any] = Field(default_factory=dict)
    reason: str


class AAPImportResult(BaseModel):
    imported: int = 0
    skipped: int = 0
    errors: list[AAPImportErrorEntry] = Field(default_factory=list)
    dry_run: bool = False


# ── helper ──────────────────────────────────────────────────────────


def check_schema_major(version: str) -> None:
    """schema_version > 当前 major 时拒绝解析, 避免静默吃下未知 breaking change."""

    try:
        major_str = version.split(".", 1)[0]
        major = int(major_str)
    except (ValueError, IndexError):
        raise ValueError(f"schema_version {version!r} 格式非法, 应形如 '1.0'")
    if major > AAP_SCHEMA_MAJOR:
        raise ValueError(
            f"AAP JSON schema_version {version!r} 超出本平台支持范围 "
            f"(当前最大支持 major={AAP_SCHEMA_MAJOR}); 请升级平台"
        )
