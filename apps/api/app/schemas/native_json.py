"""YOAO 原生交换格式：预测导入读取 predictions[]，标注导入读取 annotations[]。

文件保留导出范围内受支持的几何、模型版本、项目配置和来源。mask_objects 携带
内容寻址的 RLE，scene_tracks 记录 3D 对象的存在区间；不是对整个项目的备份。
历史文件的版本与身份在读取边界校验，业务代码使用同一个内部模型。
"""

from __future__ import annotations

from yoao_task_contract import Task as VisualTaskName

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ── schema 常量 ─────────────────────────────────────────────────────

SCHEMA_VERSION = "2.0"


# ── task_match (oneof) ───────────────────────────────────────────────


class TaskMatch(BaseModel):
    """跨实例匹配 task 的稳定键: display_id (全局唯一) + file_path (项目内)."""

    display_id: str | None = None
    file_path: str | None = None

    model_config = ConfigDict(extra="ignore")


# ── annotations / predictions entry ──────────────────────────────────


class AnnotationEntry(BaseModel):
    """导出严格写满 null；标注导入保持 lenient。"""

    geometry: dict[str, Any]
    class_name: str | None = None
    # 工具维度绑定 (1.1+). 老 1.0 reader 走 extra=ignore 容忍缺失.
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
            raise ValueError(
                "相机成员须同时提供 sensor_role、sensor_visibility、"
                "calibration_revision 和 calibration_digest"
            )
        if self.sensor_role is not None and (
            self.geometry.get("type") != "bbox" or self.track_id is None
        ):
            raise ValueError("相机成员的 geometry 须为 bbox，且必须提供 track_id")
        return self

    model_config = ConfigDict(extra="ignore")


class PredictionEntry(BaseModel):
    geometry: dict[str, Any] | None = None
    # 一个 prediction entry 可合并多个 shape 写入同一 Prediction.result.
    # 与 geometry 同时存在时 shapes 优先; 老单 geometry entry 继续兼容.
    shapes: list[dict[str, Any]] | None = None
    class_name: str | None = None
    # 工具维度绑定 (1.1+).
    tool_unit_id: str | None = None
    confidence: float | None = None
    model_version: str | None = None
    score: float | None = None  # 模型整体置信度 (与 prediction.score 对齐)
    source: str | None = None  # ml_backend / external_import
    created_at: datetime | None = None
    external_id: str | None = None  # 留 forward compat

    model_config = ConfigDict(extra="ignore")


class SceneTrackIntervalEntry(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    source: Literal["legacy_envelope", "manual", "imported", "derived"] = "imported"

    @model_validator(mode="after")
    def validate_frames(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("end_frame 不能早于 start_frame")
        return self

    model_config = ConfigDict(extra="ignore")


class SceneTrackEntry(BaseModel):
    track_id: str = Field(min_length=1, max_length=64)
    scene_name: str | None = None
    class_name: str = Field(min_length=1, max_length=100)
    presence_mode: Literal["inferred", "explicit"] = "explicit"
    attributes: dict[str, Any] = Field(default_factory=dict)
    attributes_meta: dict[str, Any] = Field(default_factory=dict)
    intervals: list[SceneTrackIntervalEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── task block ───────────────────────────────────────────────────────


class TaskBlock(BaseModel):
    task: VisualTaskName | None = None
    available_tasks: list[VisualTaskName] = Field(default_factory=list)
    task_match: TaskMatch
    file_path: str | None = None
    # 媒体维度 (1.2+). 1.x reader 走 extra=ignore, 缺失默认 "image".
    media_type: Literal["image", "video", "lidar"] = "image"
    # 视频专属元数据: 采样配置/fps/frame_count/timetable 摘要/segment/chapter.
    video: dict[str, Any] | None = None
    external_id: str | None = None
    annotations: list[AnnotationEntry] = Field(default_factory=list)
    predictions: list[PredictionEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── project / exported_from / envelope ──────────────────────────────


class ExportedFrom(BaseModel):
    platform: Literal["yoao"] = "yoao"
    platform_version: str | None = None
    project_display_id: str | None = None
    batch_display_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class ProjectMeta(BaseModel):
    name: str | None = None
    type_key: str | None = None
    # classes_config / attribute_schema 在 1.x 期间继续保留 (老 reader 派生用);
    # tool_bindings 是 1.1+ 真值字段, 1.0 reader 走 extra=ignore 容忍.
    classes_config: dict[str, Any] = Field(default_factory=dict)
    attribute_schema: dict[str, Any] = Field(default_factory=lambda: {"fields": []})
    tool_bindings: dict[str, Any] = Field(default_factory=dict)
    rendering_config: dict[str, Any] = Field(default_factory=dict)
    annotation_guide: str | None = None

    model_config = ConfigDict(extra="ignore")


class NativeJsonEnvelope(BaseModel):
    """Platform native JSON envelope.

    导出端用 model_dump(mode="json") 严格写满 null; 导入端 model_validate()
    走 extra="ignore" 实现 lenient.
    """

    schema_version: str = SCHEMA_VERSION
    format: Literal["yoao"] = "yoao"
    task: VisualTaskName | None = None
    available_tasks: list[VisualTaskName] = Field(default_factory=list)
    media_type: Literal["image", "video", "lidar"] = "image"
    source: dict[str, Any] = Field(default_factory=dict)
    exported_at: datetime | None = None
    exported_from: ExportedFrom = Field(default_factory=ExportedFrom)
    project: ProjectMeta = Field(default_factory=ProjectMeta)
    mask_objects: dict[str, dict[str, Any]] = Field(default_factory=dict)
    scene_tracks: list[SceneTrackEntry] = Field(default_factory=list)
    tasks: list[TaskBlock] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


# ── 预测与标注导入的共同回执 ───────────────────────────────────────


class ImportErrorEntry(BaseModel):
    task_match: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ImportResult(BaseModel):
    imported: int = 0
    skipped: int = 0
    errors: list[ImportErrorEntry] = Field(default_factory=list)
    dry_run: bool = False


def read_native_json_upload(content: bytes) -> NativeJsonEnvelope:
    try:
        raw = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("原生 JSON 文件必须使用 UTF-8 编码。") from None
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"原生 JSON 语法错误，请检查第 {exc.lineno} 行、第 {exc.colno} 列。"
        ) from None
    return decode_native_json(raw)


_FIELD_HINTS = {
    "missing": "缺少必填字段",
    "dict_type": "应为 JSON 对象",
    "model_type": "应为 JSON 对象",
    "list_type": "应为数组",
    "string_type": "应为字符串",
    "float_parsing": "应为数字",
    "float_type": "应为数字",
    "int_parsing": "应为整数",
    "int_type": "应为整数",
    "literal_error": "取值不在支持的选项中",
    "uuid_parsing": "应为有效的 UUID",
}


def _document_validation_detail(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(
        include_input=False, include_context=False, include_url=False
    )[:3]:
        loc = error["loc"]
        # mask_objects is keyed by uploaded object references, not schema field names.
        if loc[:1] == ("mask_objects",):
            loc = loc[:1]
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc).lstrip(
            "."
        )
        kind = error["type"]
        if kind == "value_error":
            # Cross-field validators in this schema raise fixed explanations.
            reason = error["msg"].removeprefix("Value error, ")
        else:
            reason = _FIELD_HINTS.get(kind, "请检查字段的取值和格式")
        details.append(f"{path or '文件顶层'}：{reason}")
    if exc.error_count() > len(details):
        details.append(f"另有 {exc.error_count() - len(details)} 项未列出")
    return "原生 JSON 文件结构有误：" + "；".join(details)


def decode_native_json(raw: Any) -> NativeJsonEnvelope:
    """Decode historical 1.x identities into the current internal envelope."""
    if not isinstance(raw, dict):
        raise ValueError("原生 JSON 文件的顶层必须是对象。")
    version = raw.get("schema_version")
    if not isinstance(version, str):
        raise ValueError(f'请提供字符串字段 schema_version，例如 "{SCHEMA_VERSION}"。')
    parts = version.split(".")
    if len(parts) != 2 or not all(part.isascii() and part.isdigit() for part in parts):
        raise ValueError(
            f'schema_version 须使用“主版本.次版本”格式，例如 "{SCHEMA_VERSION}"。'
        )
    major = int(parts[0])
    if major not in (1, 2):
        raise ValueError(
            "不支持此 schema_version：只能读取主版本为 1 或 2 的原生 JSON。"
        )
    identity = "aap" if major == 1 else "yoao"
    exported_from = raw.get("exported_from", {})
    if not isinstance(exported_from, dict):
        raise ValueError("原生 JSON 的 exported_from 必须是对象。")
    if raw.get("format", "aap" if major == 1 else None) != identity:
        raise ValueError(
            f'原生 JSON 主版本为 {major} 时，文件中的 format 应为 "{identity}"。'
        )
    if exported_from.get("platform", "aap" if major == 1 else None) != identity:
        raise ValueError(
            f'原生 JSON 主版本为 {major} 时，exported_from.platform 应为 "{identity}"。'
        )
    normalized = {
        **raw,
        "schema_version": SCHEMA_VERSION,
        "format": "yoao",
        "exported_from": {**exported_from, "platform": "yoao"},
    }
    try:
        return NativeJsonEnvelope.model_validate(normalized)
    except ValidationError as exc:
        raise ValueError(_document_validation_detail(exc)) from None
