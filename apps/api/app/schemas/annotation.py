from aap_task_contract import Task as VisualTaskName, normalize_task_fields
import logging

from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from uuid import UUID
from datetime import datetime
from typing import Literal

from aap_workspace.configuration_values import (
    RETIRED_AI_INTERACTIVE as _RETIRED_AI_INTERACTIVE,
    ToolUnitId as _ToolUnitId,
    map_retired_tool_unit as _map_retired_tool_unit,
)
from app.schemas._jsonb_types import (
    AnnotationAttributes,
    Geometry,
    normalize_legacy_geometry,
)

logger = logging.getLogger("app.schemas.annotation")


class AnnotationCreate(BaseModel):
    video_segment_id: UUID | None = None
    annotation_type: str = "bbox"
    # v0.10.17 · 工具单位; service 层据此校验 class_name ∈ project.tool_bindings[unit].
    # 旧调用方不传时默认 "bbox" 兼容向后.
    tool_unit_id: _ToolUnitId = "bbox"
    class_name: str
    geometry: Geometry
    confidence: float | None = None
    parent_prediction_id: UUID | None = None
    # v0.20.9 · 父子标注: 建子框时指向父框 (仅一层, service 层校验). 缺省为顶层框。
    parent_annotation_id: UUID | None = None
    lead_time: float | None = None
    attributes: AnnotationAttributes | None = None

    @model_validator(mode="before")
    @classmethod
    def _remap_retired_ai_interactive(cls, data):
        """遗留客户端 / 陈旧 worker 仍可能发 tool_unit_id='ai_interactive' (已退役)。
        按几何类型归位到 region/bbox (与迁移 0115/0116 同规则) 并记 warning, 而非 422 拒绝,
        既不让老调用方 400, 又不让退役值再污染库。"""
        if (
            isinstance(data, dict)
            and data.get("tool_unit_id") == _RETIRED_AI_INTERACTIVE
        ):
            geom = data.get("geometry")
            gtype = geom.get("type") if isinstance(geom, dict) else None
            mapped = _map_retired_tool_unit(gtype)
            logger.warning(
                "annotation create carries retired tool_unit_id 'ai_interactive'; "
                "remapping to %r by geometry type %r",
                mapped,
                gtype,
            )
            data = {**data, "tool_unit_id": mapped}
        return data

    @field_validator("geometry", mode="before")
    @classmethod
    def _normalize_legacy(cls, v):
        return normalize_legacy_geometry(v)


class AnnotationUpdate(BaseModel):
    geometry: Geometry | None = None
    class_name: str | None = None
    confidence: float | None = None
    attributes: AnnotationAttributes | None = None
    # v0.10.5 M4-β · shape 状态位（I15）
    z_order: int | None = None
    is_locked: bool | None = None
    is_hidden: bool | None = None

    @field_validator("geometry", mode="before")
    @classmethod
    def _normalize_legacy(cls, v):
        return normalize_legacy_geometry(v) if v is not None else v


class AnnotationListPage(BaseModel):
    """v0.7.6 · keyset cursor 分页响应。next_cursor=None 表示已是末页。"""

    items: list["AnnotationOut"]
    next_cursor: str | None = None


class AnnotationBulkPatch(BaseModel):
    """批量更新的字段子集.

    不允许 bulk 改 geometry (语义模糊;同一 geometry 应用到 N 个不同 shape 无意义),
    也不允许 bulk 改 tool_unit_id (会导致 class_name 校验失败).
    """

    class_name: str | None = None
    attributes: AnnotationAttributes | None = None
    z_order: int | None = None
    is_locked: bool | None = None
    is_hidden: bool | None = None


class AnnotationBulkUpdateRequest(BaseModel):
    ids: list[UUID]
    patch: AnnotationBulkPatch


class AnnotationBulkUpdateResponse(BaseModel):
    updated_ids: list[UUID]
    updated_count: int


class SecondaryInferenceRequest(BaseModel):
    """v0.20.11 · 选中框单框二次推理: 在选中框 ROI 上跑一个能力。

    镜像批量 pipeline 的单个下游阶段 (源=选中框, 无检测阶段)。
    write_target 决定产物归位: attributes → 写回原框; geometry → 建子框。
    """

    # extra=forbid: 拒绝未声明字段, 避免前端误传的隐性字段被吞后跑出"看似成功但不生效"。
    model_config = {"extra": "forbid"}

    ml_backend_id: UUID
    write_target: Literal["attributes", "geometry"] = "attributes"
    # attributes: 只取这些下游返回键 (None=全取); label 给写回键加前缀 (子命名空间)
    write_keys: list[str] | None = None
    label: str | None = None
    # backend wire 参数 (与批量下游 build_predict_context 同义)
    model_id: str | None = None
    model_variants: dict[str, str] | None = None
    params: dict | None = None
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None

    @model_validator(mode="before")
    @classmethod
    def canonical_task_fields(cls, value):
        return normalize_task_fields(value)

    prompt: str | None = None
    class_filter: list[int] | None = None
    # ROI 外扩比例 (贴边小目标留边给上下文); 上下界与批量 pipeline ROI.pad 对齐 [0, 0.5]。
    pad: float = Field(default=0.08, ge=0.0, le=0.5)


class SecondaryInferenceResponse(BaseModel):
    """更新后的原框 + 新建子框 (几何型)。属性型时 created_children 为空。"""

    annotation: "AnnotationOut"
    created_children: list["AnnotationOut"] = []


# v0.21.3 · 标注编组(Ctrl+G)持久化已删除:AnnotationGroup/Ungroup 请求响应 schema
# 随端点下线一并移除。批量编辑走 AnnotationBulkUpdate(保留)。


class VideoTrackConvertToBboxesRequest(BaseModel):
    operation: Literal["copy", "split"] = "copy"
    scope: Literal["frame", "track"] = "frame"
    frame_index: int | None = None
    frame_mode: Literal["keyframes", "all_frames"] = "keyframes"


class VideoTrackConvertToBboxesResponse(BaseModel):
    source_annotation: "AnnotationOut | None"
    created_annotations: list["AnnotationOut"]
    deleted_source: bool
    removed_frame_indexes: list[int] = []


class VideoTrackCompositionRequest(BaseModel):
    operation: Literal["aggregate_bboxes", "split_track", "merge_tracks", "join_tracks"]
    annotation_ids: list[UUID] = []
    frame_index: int | None = None
    delete_sources: bool = True
    # v0.10.30 · D-2.5 join_tracks gap 填充模式; 仅 join_tracks 使用.
    # "interpolate": gap 端点间靠现有线性插值连接, 不写 gap outside;
    # "outside": 把 gap 区间标 outside 后合并 (与 merge_tracks 默认一致).
    gap_mode: Literal["interpolate", "outside"] = "interpolate"


class VideoTrackCompositionResponse(BaseModel):
    operation: Literal["aggregate_bboxes", "split_track", "merge_tracks", "join_tracks"]
    updated_annotations: list["AnnotationOut"] = []
    created_annotations: list["AnnotationOut"] = []
    deleted_annotation_ids: list[UUID] = []


class PropagateRequest(BaseModel):
    """v0.14.1 · 跨帧目标延续: 把源 annotation 复制到目标 task(同 project 同 scene)。

    target_task_id 走 body(端点路径已含源 task_id + annotation_id)。
    override_psr 留扩展位: 本期前端总传 None = 完全复制源几何; 给定时按 key
    覆盖 box_3d 的 center/size/rotation(其它几何忽略, 高阶 propagate 用)。
    """

    target_task_id: UUID
    override_psr: dict | None = None


class PropagateResponse(BaseModel):
    annotation: "AnnotationOut"
    # v0.15.1 · box_3d 且源/目标帧均有 ego pose 时为 True(已做运动补偿);
    # False = 原样复制(无 pose 数据 / 非 box_3d / override_psr 显式给定)。
    motion_compensated: bool = False


class PropagateBatchRequest(BaseModel):
    """v0.15.1 · 多目标批量跨帧延续(源 task 走端点路径)。

    annotation_ids=None → 源 task 全部 active box_3d;显式给定时逐条校验
    归属与几何类型,任一不合法整批拒(同一事务,无部分写入)。
    """

    target_task_id: UUID
    annotation_ids: list[UUID] | None = None


class PropagateBatchItem(BaseModel):
    source_annotation_id: UUID
    annotation: "AnnotationOut"


class PropagateBatchResponse(BaseModel):
    items: list[PropagateBatchItem] = []
    motion_compensated: bool = False


class InterpolateRangeRequest(BaseModel):
    """v0.15.1 · 关键帧区间插值(from task 走端点路径)。

    同 track_id 链上两端帧各有一个 box_3d,区间内每个中间帧生成一个插值框
    (source="interpolated",便于审核过滤/批量删)。v0.21.2 · ADR-0045 · 按 track_id
    (原 group_id)标识跨帧链。
    """

    track_id: str
    to_task_id: UUID


class InterpolateRangeResponse(BaseModel):
    annotations: list["AnnotationOut"] = []
    motion_compensated: bool = False
    # 已有同 group active 标注而被幂等跳过的中间帧
    skipped_frames: list[int] = []


class AnnotationOut(BaseModel):
    id: UUID
    task_id: UUID
    project_id: UUID | None = None
    video_segment_id: UUID | None = None
    user_id: UUID | None = None
    source: str
    annotation_type: str
    # v0.10.17 · 旧记录回落默认值 "bbox" (migration 已 backfill).
    tool_unit_id: _ToolUnitId = "bbox"
    class_name: str
    geometry: Geometry
    confidence: float | None = None
    parent_prediction_id: UUID | None = None
    parent_annotation_id: UUID | None = None
    # v0.21.2 · ADR-0045 · 跨帧同一对象的通用标识 (trk_<hex>). 前端 3D 跨帧配对/
    # 插值/高亮据此认同一对象 (原用 group_id 高位段, 编组下线后该列已删).
    track_id: str | None = None
    scene_track_id: UUID | None = None
    sensor_dataset_item_id: UUID | None = None
    sensor_role: str | None = None
    sensor_visibility: Literal["visible", "occluded", "truncated", "unknown"] | None = (
        None
    )
    calibration_revision: int | None = None
    calibration_digest: str | None = None
    temporal_role: Literal["keyframe", "derived", "sample"] = "sample"
    lead_time: float | None = None
    is_active: bool
    ground_truth: bool = False
    attributes: AnnotationAttributes = {}
    # v0.20.10 · 属性级溯源 sidecar: {key: {origin, model_ref?, confidence?, at?}}.
    # 存量行 / 无 AI 属性时为 {}; 前端据此渲染 AI 属性来源 chip。
    attributes_meta: dict = {}
    # v0.10.5 M4-β · shape 状态位（I15）；旧记录回落默认值。
    z_order: int = 0
    is_locked: bool = False
    is_hidden: bool = False
    version: int = 1
    created_at: datetime
    updated_at: datetime | None = None

    @field_validator("geometry", mode="before")
    @classmethod
    def _normalize_legacy(cls, v):
        return normalize_legacy_geometry(v)

    @field_serializer("geometry", mode="wrap", when_used="json")
    def _serialize_recorded_keyframe_sources(self, geometry, handler):
        serialized = handler(geometry)
        if geometry.type in {
            "video_track_bbox",
            "video_track_polygon",
            "video_track_polyline",
            "video_track_mask",
        }:
            # Write schemas default new keyframes to manual. A read of legacy JSON
            # must not turn that default into evidence of recorded provenance.
            for keyframe, output in zip(
                geometry.keyframes, serialized["keyframes"], strict=True
            ):
                if "source" not in keyframe.model_fields_set:
                    output.pop("source", None)
        return serialized

    class Config:
        from_attributes = True


class NeighborFrameAnnotations(BaseModel):
    """v0.15.17 · 单个邻帧 task 的标注集合(若请求带 track_id 则已服务端过滤)。"""

    task_id: UUID
    frame_index: int
    annotations: list[AnnotationOut] = Field(default_factory=list)


class NeighborAnnotationsResponse(BaseModel):
    """v0.15.17 · 中心 task 前后 k 帧的邻帧标注,一次性返回。

    替代前端「对 2k 个邻帧 task 各发一条 getAnnotations + client 端按 track_id 过滤」:
    - track_id 给定 → 服务端只回该跨帧链(scope=selected,payload 最小);
    - track_id 省略 → 回区间全部框(scope=all)。
    几何坐标不变(各帧 ego 系 PSR),ego 对齐仍由前端 egoAlign 做。
    非 scene / 历史未 backfill 的 task → frames=[](不报错)。
    """

    scene_id: UUID | None = None
    frame_index: int | None = None  # 中心帧 frame_index
    frames: list[NeighborFrameAnnotations] = Field(default_factory=list)


AnnotationListPage.model_rebuild()
SecondaryInferenceResponse.model_rebuild()
VideoTrackConvertToBboxesResponse.model_rebuild()
VideoTrackCompositionResponse.model_rebuild()
PropagateResponse.model_rebuild()
