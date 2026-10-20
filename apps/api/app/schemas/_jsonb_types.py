"""v0.6.4 · 把所有 JSONB 字段的结构化 Pydantic 模型集中。

之前 ProjectOut/AnnotationOut/AnnotationCommentOut/AuditLogOut 里的 JSONB 列
被声明成 `dict` / `dict[str, Any]`，OpenAPI 自动生成的 TS 类型变成
`{ [key: string]: unknown }`，前端只能用 `Omit + 富类型` workaround 兜。

本文件把这些 shape 在后端用 Pydantic v2 声明出来：
- Geometry discriminated union（bbox / polygon）
- AnnotationAttributes（属性键值，限制元素类型）
- Mention / Attachment / CanvasDrawing（评论）
- AuditDetail（审计日志 detail_json，extra=allow + 已知字段可选）

Pydantic v2 的 discriminator 会让 codegen 在前端生成 sum type，删除全部
workaround。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from aap_assets_catalog.spatial_values import LidarAxisConvention, SensorCalibration

from app.utils.raster_mask_rle import MAX_IMAGE_MASK_DIMENSION


# ── Geometry discriminated union ────────────────────────────────────


class BboxGeometry(BaseModel):
    type: Literal["bbox"] = "bbox"
    x: float
    y: float
    w: float
    h: float

    model_config = ConfigDict(extra="allow")  # 允许 width/height 等历史别名


class VideoBboxGeometry(BaseModel):
    """v0.9.16 · 视频单帧 bbox。

    首版视频工作台只保存逐帧框，不表达 track/keyframe/interpolation。`frame_index`
    是唯一时间轴定位字段，展示层可自行换算 timecode。
    """

    type: Literal["video_bbox"] = "video_bbox"
    frame_index: int = Field(ge=0)
    x: float
    y: float
    w: float
    h: float

    model_config = ConfigDict(extra="forbid")


class VideoPolygonGeometry(BaseModel):
    """v0.21.21 · 视频单帧 polygon。与图片 PolygonGeometry 平行, 多一个 frame_index
    定位到具体帧; 与 track/keyframe/插值无关 (那是 video_track_polygon)。points 归一化 [0,1]。
    """

    type: Literal["video_polygon"] = "video_polygon"
    frame_index: int = Field(ge=0)
    points: list[list[float]] = Field(min_length=3)
    holes: list[list[list[float]]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v

    @field_validator("holes")
    @classmethod
    def _check_holes(cls, v: list[list[list[float]]]) -> list[list[list[float]]]:
        for hi, hole in enumerate(v):
            if len(hole) < 3:
                raise ValueError(f"holes[{hi}] 顶点 < 3, 不构成有效环")
            for pi, pt in enumerate(hole):
                if len(pt) != 2:
                    raise ValueError(f"holes[{hi}][{pi}] 必须是 [x, y]")
        return v


class VideoPolylineGeometry(BaseModel):
    """v0.21.21 · 视频单帧开放折线 (不闭合)。与图片 PolylineGeometry 平行 + frame_index;
    points 至少 2 个归一化 [0,1] 顶点。
    """

    type: Literal["video_polyline"] = "video_polyline"
    frame_index: int = Field(ge=0)
    points: list[list[float]] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v


class VideoRotatedBboxGeometry(BaseModel):
    """v0.21.22 · 视频单帧旋转矩形 (OBB)。与图片 RotatedBboxGeometry 平行 + frame_index;
    cx,cy 中心, w,h 边长, angle 顺时针角度 (度, [0,360))。坐标归一化 [0,1]。
    """

    type: Literal["video_rotated_bbox"] = "video_rotated_bbox"
    frame_index: int = Field(ge=0)
    cx: float
    cy: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)
    angle: float = Field(ge=0, lt=360)

    model_config = ConfigDict(extra="forbid")


class VideoTrackBbox(BaseModel):
    x: float
    y: float
    w: float
    h: float

    model_config = ConfigDict(extra="forbid")


class VideoTrackKeyframe(BaseModel):
    frame_index: int = Field(ge=0)
    bbox: VideoTrackBbox
    source: Literal["manual", "interpolated", "prediction"] = "manual"
    occluded: bool = False
    # v0.10.6 M4-γ · I13.2 · mutable attribute 的逐帧覆盖。仅承载 schema 里
    # `mutable=true` 的属性键值；为 None 时表示该帧用 annotation.attributes
    # （track 默认值）。前端 PATCH 时只动这一字段，不污染 track 整体 attributes。
    attributes: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class VideoTrackOutsideRange(BaseModel):
    from_: int = Field(alias="from", ge=0)
    to: int = Field(ge=0)
    source: Literal["manual", "prediction"] = "manual"

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class VideoTrackGeometry(BaseModel):
    """v0.9.17 · 视频对象轨迹。

    轨迹以 compact JSON 保存，不逐帧展开写库。`track_id` 在一个 annotation 内稳定；
    `keyframes` 保存手工 / 预测关键帧，插值结果由前端按需计算。
    """

    type: Literal["video_track_bbox"] = "video_track_bbox"
    track_id: str = Field(min_length=1, max_length=64)
    # v0.10.30 · 2.1 用户可编辑的语义标签 (如 "car_3"), 仅作跨任务 Re-ID 心智,
    # 不参与主键、不强制唯一。track_number 由 derive_track_number 确定性派生, 不持久化。
    semantic_label: str | None = None
    keyframes: list[VideoTrackKeyframe] = Field(min_length=1)
    outside: list[VideoTrackOutsideRange] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class VideoTrackPolygonKeyframe(BaseModel):
    """v0.21.20 · polygon track 的单关键帧。

    `points` 为归一化 [0,1] 的闭合多边形顶点序列 (与图片 PolygonGeometry 同形状,
    不含 holes——track 首切片只做单连通)。帧间由弧长参数化插值 (见 video_tracks.lerp_polygon)。
    """

    frame_index: int = Field(ge=0)
    points: list[list[float]] = Field(min_length=3)
    source: Literal["manual", "interpolated", "prediction"] = "manual"
    occluded: bool = False
    # 与 VideoTrackKeyframe.attributes 同义: mutable 属性的逐帧覆盖。
    attributes: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v


class VideoTrackPolygonGeometry(BaseModel):
    """v0.21.20 · 视频对象轨迹 (polygon 几何)。

    与 `video_track_bbox` 平行的附加类型 (非泛化 bbox), 复用 track_id / semantic_label /
    outside 语义; keyframes 存 polygon 顶点, 帧间按弧长参数化插值 (顶点数不等时先重采样到
    公共参数化)。存量 bbox track 零迁移、零改动。
    """

    type: Literal["video_track_polygon"] = "video_track_polygon"
    track_id: str = Field(min_length=1, max_length=64)
    semantic_label: str | None = None
    keyframes: list[VideoTrackPolygonKeyframe] = Field(min_length=1)
    outside: list[VideoTrackOutsideRange] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class VideoTrackPolylineKeyframe(BaseModel):
    """v0.21.20 · polyline track 的单关键帧。

    `points` 为归一化 [0,1] 的开路径折线顶点序列 (不闭合, 至少 2 点)。帧间由开路径弧长
    参数化插值 (首尾端点固定对应, 见 video_tracks.lerp_polyline)。
    """

    frame_index: int = Field(ge=0)
    points: list[list[float]] = Field(min_length=2)
    source: Literal["manual", "interpolated", "prediction"] = "manual"
    occluded: bool = False
    attributes: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v


class VideoTrackPolylineGeometry(BaseModel):
    """v0.21.20 · 视频对象轨迹 (polyline / 开路径几何)。

    与 `video_track_polygon` 平行, 但 keyframes 为不闭合折线 (无 holes / 无闭合边);
    帧间按开路径弧长参数化插值。存量轨迹零迁移。
    """

    type: Literal["video_track_polyline"] = "video_track_polyline"
    track_id: str = Field(min_length=1, max_length=64)
    semantic_label: str | None = None
    keyframes: list[VideoTrackPolylineKeyframe] = Field(min_length=1)
    outside: list[VideoTrackOutsideRange] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class CocoRleMaskRef(BaseModel):
    """Immutable content-addressed COCO uncompressed RLE object reference."""

    encoding: Literal["coco_rle_ref", "coco_rle_gzip"] = "coco_rle_ref"
    storage_encoding: Literal["identity", "gzip"] | None = None
    size: tuple[StrictInt, StrictInt]
    object_key: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^raster-masks/sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.json(?:\.gz)?$",
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runs: StrictInt = Field(ge=1, le=1_000_000)
    bytes: StrictInt = Field(ge=1, le=4 * 1024 * 1024)

    model_config = ConfigDict(extra="forbid")

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        height, width = value
        if height <= 0 or width <= 0:
            raise ValueError("mask size 必须是正整数 [height, width]")
        if height > MAX_IMAGE_MASK_DIMENSION or width > MAX_IMAGE_MASK_DIMENSION:
            raise ValueError(f"mask width / height 必须 <= {MAX_IMAGE_MASK_DIMENSION}")
        return value

    @model_validator(mode="after")
    def _check_content_address(self):
        gzip_key = self.object_key.endswith(".json.gz")
        expected = (
            f"raster-masks/sha256/{self.sha256[:2]}/{self.sha256[2:4]}/"
            f"{self.sha256}.json"
        )
        expected = f"{expected}.gz" if gzip_key else expected
        if self.object_key != expected:
            raise ValueError("mask object_key 必须与 sha256 内容地址一致")
        if self.storage_encoding == "gzip" and not gzip_key:
            raise ValueError("gzip mask reference 必须使用 .json.gz")
        if self.storage_encoding == "identity" and gzip_key:
            raise ValueError("identity mask reference 必须使用 .json")
        if self.encoding == "coco_rle_gzip" and not gzip_key:
            raise ValueError("legacy coco_rle_gzip reference 必须使用 .json.gz")
        if (
            gzip_key
            and self.encoding == "coco_rle_ref"
            and self.storage_encoding != "gzip"
        ):
            raise ValueError(".json.gz mask reference 必须声明 storage_encoding=gzip")
        return self


class CocoRleContent(BaseModel):
    """Materialized uncompressed COCO RLE returned by mask-content APIs."""

    encoding: Literal["coco_rle"] = "coco_rle"
    size: tuple[StrictInt, StrictInt]
    counts: list[StrictInt] = Field(min_length=1, max_length=1_000_000)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_rle_contract(self):
        height, width = self.size
        if (
            height <= 0
            or width <= 0
            or height > MAX_IMAGE_MASK_DIMENSION
            or width > MAX_IMAGE_MASK_DIMENSION
        ):
            raise ValueError(
                f"mask size 必须是 (0, {MAX_IMAGE_MASK_DIMENSION}] 内的 [height, width]"
            )
        total = 0
        for count in self.counts:
            if count < 0:
                raise ValueError("mask counts 必须是非负整数")
            total += count
            if total > height * width:
                raise ValueError("mask counts 总和超过 height * width")
        if total != height * width:
            raise ValueError("mask counts 总和必须等于 height * width")
        return self


class VideoMaskGeometry(BaseModel):
    """Single-frame video raster mask backed by immutable COCO RLE content."""

    type: Literal["video_mask"] = "video_mask"
    frame_index: StrictInt = Field(ge=0)
    mask: CocoRleMaskRef

    model_config = ConfigDict(extra="forbid")


class VideoTrackMaskKeyframe(BaseModel):
    frame_index: StrictInt = Field(ge=0)
    mask: CocoRleMaskRef
    source: Literal["manual", "prediction"] = "manual"
    occluded: bool = False
    attributes: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class VideoTrackMaskGeometry(BaseModel):
    """Video raster-mask track; keyframes reference immutable COCO RLE objects."""

    type: Literal["video_track_mask"] = "video_track_mask"
    track_id: str = Field(min_length=1, max_length=64)
    semantic_label: str | None = None
    keyframes: list[VideoTrackMaskKeyframe] = Field(min_length=1, max_length=3000)
    outside: list[VideoTrackOutsideRange] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_track_contract(self):
        frames = [keyframe.frame_index for keyframe in self.keyframes]
        if frames != sorted(frames) or len(frames) != len(set(frames)):
            raise ValueError("mask keyframes 必须按 frame_index 严格递增且唯一")
        if any(range_.from_ > range_.to for range_ in self.outside):
            raise ValueError("outside range 必须满足 from <= to")
        if len(self.model_dump_json().encode()) > 8 * 1024 * 1024:
            raise ValueError("mask track geometry JSON 必须 <= 8 MiB")
        return self


class RasterMaskGeometry(BaseModel):
    """v0.23.6 · 图片栅格掩码几何。

    引用不可变 COCO RLE 对象存储掩码内容，不内联像素数据。
    只允许图片 DatasetItem（视频使用 video_mask / video_track_mask）。
    """

    type: Literal["raster_mask"] = "raster_mask"
    mask: CocoRleMaskRef

    model_config = ConfigDict(extra="forbid")


class PolygonGeometry(BaseModel):
    """单连通域 polygon。

    v0.9.14 · holes 字段新增, 默认 [] 向后兼容。老存量 / 老前端写入仍走 type=polygon
    + 仅 points 路径; 新 prediction 在有 hole 时把 hole 顶点列表填进 holes, 多连通域
    走 MultiPolygonGeometry 分支。
    """

    type: Literal["polygon"] = "polygon"
    points: list[list[float]] = Field(min_length=3)
    holes: list[list[list[float]]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v

    @field_validator("holes")
    @classmethod
    def _check_holes(cls, v: list[list[list[float]]]) -> list[list[list[float]]]:
        for hi, hole in enumerate(v):
            if len(hole) < 3:
                raise ValueError(f"holes[{hi}] 顶点 < 3, 不构成有效环")
            for pi, pt in enumerate(hole):
                if len(pt) != 2:
                    raise ValueError(f"holes[{hi}][{pi}] 必须是 [x, y]")
        return v


class MultiPolygonGeometry(BaseModel):
    """多连通域 polygon 集合。每个 polygons[i] 内部仍是带 hole 的单连通 PolygonGeometry。

    v0.9.14 · 配合 mask_to_multi_polygon (apps/_shared/mask_utils) 输出。Predictor 在
    单连通无 hole 时仍输出 PolygonGeometry 兼容老前端; 多连通或带 hole 才走本分支。
    """

    type: Literal["multi_polygon"] = "multi_polygon"
    polygons: list[PolygonGeometry] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


# ── v0.10.28 · rotated_bbox / polyline / keypoint 几何（坐标归一化 [0,1]） ──


class RotatedBboxGeometry(BaseModel):
    """旋转矩形。cx,cy 为中心点，w,h 为边长，angle 为顺时针旋转角度（度，[0,360)）。"""

    type: Literal["rotated_bbox"] = "rotated_bbox"
    cx: float
    cy: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)
    angle: float = Field(ge=0, lt=360)

    model_config = ConfigDict(extra="forbid")


class PolylineGeometry(BaseModel):
    """开放折线（不闭合）。points 至少 2 个 [x, y] 顶点。"""

    type: Literal["polyline"] = "polyline"
    points: list[list[float]] = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for i, pt in enumerate(v):
            if len(pt) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
        return v


class Keypoint(BaseModel):
    """单个关键点。v 为 COCO 可见性：0 未标注 / 1 遮挡 / 2 可见。"""

    x: float
    y: float
    v: int

    model_config = ConfigDict(extra="forbid")

    @field_validator("v")
    @classmethod
    def _check_visibility(cls, v: int) -> int:
        if v not in {0, 1, 2}:
            raise ValueError("v 必须是 0(未标注)/1(遮挡)/2(可见) 之一")
        return v


class KeypointGeometry(BaseModel):
    """关键点集合实例几何。骨骼拓扑（节点名/连线）走类别级 ToolBinding.keypoint_schema。"""

    type: Literal["keypoint"] = "keypoint"
    points: list[Keypoint] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class VideoKeypointGeometry(BaseModel):
    """v0.21.22 · 视频单帧关键点集合。与图片 KeypointGeometry 平行 + frame_index;
    骨骼拓扑走 keypoint 工具单位的 keypoint_schema。坐标归一化 [0,1]。
    """

    type: Literal["video_keypoint"] = "video_keypoint"
    frame_index: int = Field(ge=0)
    points: list[Keypoint] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


# ── v0.13.0 · 点云 3D 几何（box_3d / point_mask_3d） ──────────────────


class Box3DGeometry(BaseModel):
    """v0.13.0 · LiDAR 3D 框。center/size/rotation 各为长度 3 的列表
    (x,y,z 米 / 长宽高 / 绕各轴弧度)。extra="allow" 容纳标定/属性等扩展字段。"""

    type: Literal["box_3d"] = "box_3d"
    center: list[float] = Field(min_length=3, max_length=3)
    size: list[float] = Field(min_length=3, max_length=3)
    rotation: list[float] = Field(min_length=3, max_length=3)
    convention_at_create: LidarAxisConvention | None = None

    model_config = ConfigDict(extra="allow")


class PointMaskGeometry(BaseModel):
    """v0.13.0 · 3D 点云语义/实例分割掩码。point_indices 为指向点云的整数索引列表。"""

    type: Literal["point_mask_3d"] = "point_mask_3d"
    # 上界防止单条标注 geometry 膨胀到几 MB（jsonb / 列表序列化 / AAP 导出都会被放大）。
    # 前端渲染抽稀到 DECIMATE_THRESHOLD=500k 点，全选最多 ~500k 索引，600k 留足余量。
    point_indices: list[int] = Field(default_factory=list, max_length=600_000)
    convention_at_create: LidarAxisConvention | None = None
    decimate_stride: int | None = Field(default=None, ge=1)
    source_point_count: int | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid")

    @field_validator("point_indices")
    @classmethod
    def _check_non_negative(cls, v: list[int]) -> list[int]:
        if any(i < 0 for i in v):
            raise ValueError("point_indices 必须全为非负整数")
        return v


Geometry = Annotated[
    BboxGeometry
    | VideoBboxGeometry
    | VideoPolygonGeometry
    | VideoPolylineGeometry
    | VideoKeypointGeometry
    | VideoRotatedBboxGeometry
    | VideoTrackGeometry
    | VideoTrackPolygonGeometry
    | VideoTrackPolylineGeometry
    | VideoMaskGeometry
    | VideoTrackMaskGeometry
    | PolygonGeometry
    | MultiPolygonGeometry
    | RotatedBboxGeometry
    | PolylineGeometry
    | KeypointGeometry
    | Box3DGeometry
    | PointMaskGeometry
    | RasterMaskGeometry,
    Field(discriminator="type"),
]


def normalize_legacy_geometry(g: Any) -> Any:
    """旧 bbox 写入时不带 type，这里补 type='bbox' 兼容历史 DB 数据。

    在 from-DB 路径（AnnotationOut）和 from-API 路径（AnnotationCreate）的
    `field_validator(mode="before")` 都用一遍。
    """
    if (
        isinstance(g, dict)
        and g.get("type") is None
        and {"x", "y", "w", "h"}.issubset(g.keys())
    ):
        return {**g, "type": "bbox"}
    return g


# ── Annotation attributes（属性键值） ───────────────────────────────

# 属性值类型受限：基础标量 + None + 字符串列表（multiselect）
AnnotationAttributeValue = str | int | float | bool | None | list[str]
AnnotationAttributes = dict[str, AnnotationAttributeValue]


# ── 评论：mentions / attachments / canvas_drawing ──────────────────

ATTACHMENT_KEY_PREFIX = "comment-attachments/"


class Mention(BaseModel):
    user_id: UUID = Field(alias="userId")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    offset: int = Field(ge=0)
    length: int = Field(ge=1)

    model_config = ConfigDict(populate_by_name=True)


class Attachment(BaseModel):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=128)
    size: int = Field(ge=0)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("storage_key")
    @classmethod
    def _validate_prefix(cls, v: str) -> str:
        if not v.startswith(ATTACHMENT_KEY_PREFIX):
            raise ValueError(
                f"attachments[].storageKey 必须以 {ATTACHMENT_KEY_PREFIX!r} 开头"
            )
        return v


CanvasShapeType = Literal["line", "arrow", "rect", "ellipse"]


class CanvasShape(BaseModel):
    type: CanvasShapeType
    points: list[float] = Field(min_length=2)
    stroke: str | None = None
    # v0.10.21 I4 · 笔画 timeline. 全 Optional, 旧记录缺字段时 UI 降级不渲染时间条.
    # id: client uuid; started_at/ended_at: ms epoch.
    id: str | None = None
    started_at: float | None = None
    ended_at: float | None = None

    model_config = ConfigDict(extra="forbid")


class CanvasDrawing(BaseModel):
    """Reviewer 端 Konva overlay 序列化的批注笔触集合（归一化坐标 [0,1]）。"""

    shapes: list[CanvasShape] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class CommentAnchor(BaseModel):
    """评论锚点。v0.9.35 起用于视频 review 的帧级定位。"""

    kind: Literal["video_frame"]
    frame_index: int = Field(ge=0, alias="frameIndex")
    track_id: str | None = Field(default=None, alias="trackId", max_length=120)
    source: Literal["manual", "prediction", "interpolated", "legacy"] | None = None

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# ── AuditLog detail_json ────────────────────────────────────────────


class AuditDetail(BaseModel):
    """通用审计 detail：所有 23 种 action 的 detail_json shape。

    common：所有写请求都带 request_id（middleware 注入）
    业务字段：随 action 不同而不同；用 extra="allow" 容纳，
    几个高频字段单列出来好让 codegen 生成强类型 hint。

    后续按需要把高频 action 拆成自己的 BaseModel + discriminated union。
    """

    request_id: str | None = None

    # AnnotationAttributeChange（高频）
    task_id: str | None = None
    field_key: str | None = None
    before: Any | None = None
    after: Any | None = None

    # UserProfileUpdate
    old_name: str | None = None
    new_name: str | None = None

    model_config = ConfigDict(extra="allow")


class DatasetItemMetadata(BaseModel):
    """v0.13.1 · DatasetItem.metadata_ 的结构化视图。calibration 仅点云相机项有;
    extra="allow" 保留其它历史/未来 metadata key 不丢。"""

    calibration: SensorCalibration | None = None

    model_config = ConfigDict(extra="allow")


class DatasetMetadata(BaseModel):
    """v0.13.11 · Dataset.metadata_ 的结构化视图 (extra="allow" 留给未来扩展)。"""

    axis_convention: LidarAxisConvention | None = None

    model_config = ConfigDict(extra="allow")
