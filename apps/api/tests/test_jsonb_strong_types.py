"""v0.6.4 · Pydantic JSONB 字段强类型化的回归测试。

把以前散落在 dict 里的 shape 拉成 Pydantic 模型后，应该：
- 合法 shape 通过
- 非法 shape 422
- 历史 bbox（不带 type）能 normalize 通过 OUT 路径
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_assets_catalog.spatial_values import SensorCalibration

from aap_workspace.configuration_values import (
    AttributeField,
    AttributeSchema,
    ClassConfigEntry,
    KeypointNode,
    KeypointSchema,
    ToolBinding,
    VideoModesConfig,
)
from app.schemas._jsonb_types import (
    AnnotationAttributes,
    Attachment,
    AuditDetail,
    BboxGeometry,
    Box3DGeometry,
    CanvasDrawing,
    CanvasShape,
    CocoRleContent,
    CocoRleMaskRef,
    DatasetItemMetadata,
    Geometry,
    Keypoint,
    KeypointGeometry,
    Mention,
    PointMaskGeometry,
    PolygonGeometry,
    PolylineGeometry,
    RotatedBboxGeometry,
    VideoTrackBbox,
    VideoTrackGeometry,
    VideoTrackKeyframe,
    VideoTrackPolygonGeometry,
    VideoTrackPolygonKeyframe,
    VideoTrackPolylineGeometry,
    VideoTrackPolylineKeyframe,
    VideoMaskGeometry,
    VideoTrackMaskGeometry,
)


# ── Geometry ────────────────────────────────────────────────────────


def test_bbox_geometry_required_fields():
    g = BboxGeometry(x=0.1, y=0.2, w=0.3, h=0.4)
    assert g.type == "bbox"
    assert g.x == 0.1


def test_polygon_geometry_min_3_points():
    PolygonGeometry(points=[[0, 0], [1, 0], [1, 1]])
    with pytest.raises(ValidationError):
        PolygonGeometry(points=[[0, 0], [1, 0]])


def test_polygon_geometry_pair_shape():
    with pytest.raises(ValidationError):
        PolygonGeometry(points=[[0, 0, 0], [1, 0, 0], [1, 1, 0]])


# ── v0.10.28 · rotated_bbox / polyline / keypoint ────────────────────


def test_rotated_bbox_geometry_valid():
    g = RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=0.1, angle=45)
    assert g.type == "rotated_bbox"
    assert g.angle == 45
    # 边界: angle=0 合法, 359.9 合法
    RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=0.1, angle=0)
    RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=0.1, angle=359.9)


def test_rotated_bbox_geometry_invalid():
    # w/h 必须 > 0
    with pytest.raises(ValidationError):
        RotatedBboxGeometry(cx=0.5, cy=0.5, w=0, h=0.1, angle=10)
    with pytest.raises(ValidationError):
        RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=-0.1, angle=10)
    # angle 必须 [0, 360)
    with pytest.raises(ValidationError):
        RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=0.1, angle=360)
    with pytest.raises(ValidationError):
        RotatedBboxGeometry(cx=0.5, cy=0.5, w=0.2, h=0.1, angle=-1)


def test_polyline_geometry_valid():
    g = PolylineGeometry(points=[[0, 0], [0.5, 0.5]])
    assert g.type == "polyline"
    assert len(g.points) == 2


def test_polyline_geometry_invalid():
    # 至少 2 点
    with pytest.raises(ValidationError):
        PolylineGeometry(points=[[0, 0]])
    # 每点必须 [x, y]
    with pytest.raises(ValidationError):
        PolylineGeometry(points=[[0, 0], [1, 1, 1]])


def test_keypoint_geometry_valid():
    g = KeypointGeometry(points=[Keypoint(x=0.1, y=0.2, v=2)])
    assert g.type == "keypoint"
    assert g.points[0].v == 2
    # v 取 0/1/2 都合法
    KeypointGeometry(points=[Keypoint(x=0, y=0, v=0), Keypoint(x=1, y=1, v=1)])


def test_keypoint_geometry_invalid():
    # v 必须 in {0,1,2}
    with pytest.raises(ValidationError):
        Keypoint(x=0.1, y=0.2, v=3)
    # 至少 1 个关键点
    with pytest.raises(ValidationError):
        KeypointGeometry(points=[])


def test_keypoint_schema_valid():
    s = KeypointSchema(
        nodes=[KeypointNode(name="nose", color="#ff0000"), KeypointNode(name="eye")],
        edges=[[0, 1]],
    )
    assert len(s.nodes) == 2
    assert s.edges == [[0, 1]]


def test_keypoint_schema_invalid_edge():
    # edge 必须长度 2
    with pytest.raises(ValidationError):
        KeypointSchema(nodes=[KeypointNode(name="a")], edges=[[0]])
    # edge 索引 >= 0
    with pytest.raises(ValidationError):
        KeypointSchema(nodes=[KeypointNode(name="a")], edges=[[0, -1]])
    # node color 必须 #RRGGBB
    with pytest.raises(ValidationError):
        KeypointNode(name="a", color="red")


def test_keypoint_node_sublabel_and_template_coords():
    # sublabel + 模板坐标 (归一化) 均合法且默认 None
    n0 = KeypointNode(name="shoulder")
    assert n0.sublabel is None and n0.x is None and n0.y is None
    n = KeypointNode(name="shoulder", sublabel="left", x=0.25, y=0.75)
    assert n.sublabel == "left"
    assert n.x == 0.25 and n.y == 0.75


def test_keypoint_node_template_coords_out_of_range():
    # x / y 必须在 [0, 1]
    with pytest.raises(ValidationError):
        KeypointNode(name="a", x=1.5)
    with pytest.raises(ValidationError):
        KeypointNode(name="a", y=-0.1)


def test_tool_binding_keypoint_schema_optional():
    # 默认 None
    b = ToolBinding()
    assert b.keypoint_schema is None
    # keypoint 单元可携带骨骼拓扑
    b2 = ToolBinding(
        enabled=True,
        keypoint_schema=KeypointSchema(
            nodes=[KeypointNode(name="nose"), KeypointNode(name="eye")],
            edges=[[0, 1]],
        ),
    )
    assert b2.keypoint_schema is not None
    assert len(b2.keypoint_schema.nodes) == 2


def test_video_modes_config_at_least_one_enabled():
    # 默认单帧 / 轨迹变体均可用
    assert VideoModesConfig() == VideoModesConfig(box=True, track=True)
    # 单独保留任一合法
    assert VideoModesConfig(box=True, track=False).box is True
    assert VideoModesConfig(box=False, track=True).track is True
    # 单帧 / 轨迹都 false 才非法：该几何单位 enabled 却什么都画不了
    with pytest.raises(ValidationError):
        VideoModesConfig(box=False, track=False)


# ── Attribute schema ────────────────────────────────────────────────


def test_attribute_schema_unique_keys():
    AttributeSchema(
        fields=[
            AttributeField(key="a", label="A", type="text"),
            AttributeField(key="b", label="B", type="text"),
        ]
    )
    with pytest.raises(ValidationError):
        AttributeSchema(
            fields=[
                AttributeField(key="a", label="A", type="text"),
                AttributeField(key="a", label="B", type="text"),
            ]
        )


def test_attribute_schema_hotkey_constraints():
    # hotkey 字符必须 1-9
    with pytest.raises(ValidationError):
        AttributeField(key="x", label="X", type="boolean", hotkey="0")
    # hotkey 仅 boolean / select
    with pytest.raises(ValidationError):
        AttributeSchema(
            fields=[
                AttributeField(key="x", label="X", type="text", hotkey="1"),
            ]
        )
    # 重复 hotkey
    with pytest.raises(ValidationError):
        AttributeSchema(
            fields=[
                AttributeField(key="a", label="A", type="boolean", hotkey="1"),
                AttributeField(key="b", label="B", type="boolean", hotkey="1"),
            ]
        )


# ── v0.10.6 M4-γ · I13.2 mutable / immutable ──────────────────────────


def test_attribute_field_mutable_defaults_unset():
    """旧 schema 不带 mutable 字段时序列化后仍是 None（向后兼容）。"""
    f = AttributeField(key="color", label="Color", type="text")
    assert f.mutable is None
    # 同样接受显式 False / True
    assert (
        AttributeField(key="o", label="O", type="boolean", mutable=True).mutable is True
    )
    assert (
        AttributeField(key="c", label="C", type="text", mutable=False).mutable is False
    )


def test_attribute_schema_mixed_mutable_immutable():
    """同一 schema 内 mutable 与 immutable 共存，互不影响。"""
    s = AttributeSchema(
        fields=[
            AttributeField(
                key="vehicle_color",
                label="车身颜色",
                type="select",
                options=[{"value": "red", "label": "红"}],
                mutable=False,
            ),
            AttributeField(key="occluded", label="遮挡", type="boolean", mutable=True),
        ]
    )
    by_key = {f.key: f for f in s.fields}
    assert by_key["vehicle_color"].mutable is False
    assert by_key["occluded"].mutable is True


def test_video_track_keyframe_attributes_optional():
    """keyframe.attributes 不写默认 None，可选地承载 mutable 属性覆盖。"""
    kf = VideoTrackKeyframe(frame_index=0, bbox=VideoTrackBbox(x=0, y=0, w=1, h=1))
    assert kf.attributes is None

    kf2 = VideoTrackKeyframe(
        frame_index=5,
        bbox=VideoTrackBbox(x=0, y=0, w=1, h=1),
        attributes={"occluded": True, "orientation": "left"},
    )
    assert kf2.attributes == {"occluded": True, "orientation": "left"}


def test_video_track_geometry_keyframes_with_overrides():
    """完整 VideoTrackGeometry 包含 attributes override 仍能通过校验。"""
    geom = VideoTrackGeometry(
        track_id="t1",
        keyframes=[
            VideoTrackKeyframe(frame_index=0, bbox=VideoTrackBbox(x=0, y=0, w=1, h=1)),
            VideoTrackKeyframe(
                frame_index=3,
                bbox=VideoTrackBbox(x=0, y=0, w=1, h=1),
                attributes={"occluded": True},
            ),
        ],
    )
    assert geom.keyframes[0].attributes is None
    assert geom.keyframes[1].attributes == {"occluded": True}


def test_video_track_polygon_geometry_valid():
    """v0.21.20 · polygon track: keyframe 存归一化 points, 与 bbox track 平行。"""
    geom = VideoTrackPolygonGeometry(
        track_id="p1",
        keyframes=[
            VideoTrackPolygonKeyframe(
                frame_index=0, points=[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2]]
            ),
            VideoTrackPolygonKeyframe(
                frame_index=5,
                points=[[0.4, 0.0], [0.6, 0.0], [0.6, 0.2]],
                attributes={"occluded": True},
            ),
        ],
    )
    assert geom.type == "video_track_polygon"
    assert geom.keyframes[0].points[1] == [0.2, 0.0]
    assert geom.keyframes[1].attributes == {"occluded": True}


def test_video_track_polygon_keyframe_min_3_points():
    with pytest.raises(ValidationError):
        VideoTrackPolygonKeyframe(frame_index=0, points=[[0.0, 0.0], [0.1, 0.1]])


def test_video_track_polygon_keyframe_pair_shape():
    with pytest.raises(ValidationError):
        VideoTrackPolygonKeyframe(
            frame_index=0, points=[[0.0, 0.0], [0.1, 0.1], [0.2, 0.2, 0.3]]
        )


def test_video_track_polyline_geometry_valid():
    """v0.21.20 · polyline track: 开路径 (min 2 点), 与 polygon 平行。"""
    geom = VideoTrackPolylineGeometry(
        track_id="l1",
        keyframes=[
            VideoTrackPolylineKeyframe(frame_index=0, points=[[0.0, 0.0], [0.4, 0.0]]),
            VideoTrackPolylineKeyframe(frame_index=5, points=[[0.0, 0.2], [0.4, 0.2]]),
        ],
    )
    assert geom.type == "video_track_polyline"
    assert geom.keyframes[0].points == [[0.0, 0.0], [0.4, 0.0]]


def test_video_track_polyline_keyframe_min_2_points():
    with pytest.raises(ValidationError):
        VideoTrackPolylineKeyframe(frame_index=0, points=[[0.0, 0.0]])


def test_video_track_polyline_geometry_in_union():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Geometry)
    parsed = adapter.validate_python(
        {
            "type": "video_track_polyline",
            "track_id": "l1",
            "keyframes": [{"frame_index": 0, "points": [[0.0, 0.0], [0.4, 0.0]]}],
        }
    )
    assert isinstance(parsed, VideoTrackPolylineGeometry)


def test_video_track_polygon_geometry_in_union():
    """polygon track 能经 Geometry 判别联合按 type 解析。"""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Geometry)
    parsed = adapter.validate_python(
        {
            "type": "video_track_polygon",
            "track_id": "p1",
            "keyframes": [
                {"frame_index": 0, "points": [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2]]}
            ],
        }
    )
    assert isinstance(parsed, VideoTrackPolygonGeometry)


def _mask_ref(*, sha: str = "a" * 64) -> dict:
    return {
        "encoding": "coco_rle_ref",
        "size": [1080, 1920],
        "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
        "sha256": sha,
        "runs": 953,
        "bytes": 3659,
    }


def test_video_track_mask_geometry_valid():
    geometry = VideoTrackMaskGeometry.model_validate(
        {
            "type": "video_track_mask",
            "track_id": "mask-1",
            "keyframes": [
                {"frame_index": 2, "mask": _mask_ref(), "source": "manual"},
                {
                    "frame_index": 8,
                    "mask": _mask_ref(sha="b" * 64),
                    "source": "prediction",
                },
            ],
            "outside": [{"from": 4, "to": 5}],
        }
    )
    assert geometry.keyframes[0].mask.encoding == "coco_rle_ref"


def test_video_mask_geometry_valid_and_rejects_non_integer_frame():
    geometry = VideoMaskGeometry.model_validate(
        {"type": "video_mask", "frame_index": 2, "mask": _mask_ref()}
    )
    assert geometry.mask.encoding == "coco_rle_ref"
    with pytest.raises(ValidationError):
        VideoMaskGeometry.model_validate(
            {"type": "video_mask", "frame_index": True, "mask": _mask_ref()}
        )


@pytest.mark.parametrize(
    "patch",
    [
        {
            "keyframes": [
                {"frame_index": 2, "mask": _mask_ref()},
                {"frame_index": 2, "mask": _mask_ref(sha="b" * 64)},
            ]
        },
        {"keyframes": [{"frame_index": True, "mask": _mask_ref()}]},
        {"keyframes": [{"frame_index": 2, "mask": {**_mask_ref(), "size": [8193, 1]}}]},
        {"keyframes": [{"frame_index": 2, "mask": {**_mask_ref(), "runs": 1_000_001}}]},
        {
            "keyframes": [
                {"frame_index": 2, "mask": {**_mask_ref(), "sha256": "b" * 64}}
            ]
        },
        {"outside": [{"from": 5, "to": 4}]},
    ],
)
def test_video_track_mask_geometry_rejects_invalid_contract(patch):
    payload = {
        "type": "video_track_mask",
        "track_id": "mask-1",
        "keyframes": [{"frame_index": 2, "mask": _mask_ref()}],
        **patch,
    }
    with pytest.raises(ValidationError):
        VideoTrackMaskGeometry.model_validate(payload)


def test_shared_rle_models_accept_structural_8k_envelope():
    reference = {**_mask_ref(), "size": [8192, 8192]}
    assert CocoRleMaskRef.model_validate(reference).size == (8192, 8192)
    assert CocoRleContent.model_validate(
        {
            "encoding": "coco_rle",
            "size": [8192, 8192],
            "counts": [67_108_864],
        }
    ).size == (8192, 8192)


# ── ClassConfigEntry alias（v0.9.5）────────────────────────────────


def test_class_config_alias_optional_default_none():
    e = ClassConfigEntry(color="#ff0000", order=0)
    assert e.alias is None


def test_class_config_alias_ascii_allowed():
    e = ClassConfigEntry(alias="ripe apple")
    assert e.alias == "ripe apple"
    e2 = ClassConfigEntry(alias="cat,dog,bird")
    assert e2.alias == "cat,dog,bird"


def test_class_config_alias_rejects_chinese():
    with pytest.raises(ValidationError):
        ClassConfigEntry(alias="苹果")


def test_class_config_alias_rejects_overlong():
    with pytest.raises(ValidationError):
        ClassConfigEntry(alias="x" * 51)


# ── v0.9.6 · alias 规范化 ───────────────────────────────────────────


def test_class_config_alias_lowercased():
    """v0.9.6 · DINO 召回更稳, 前端用户输大小写都规范化为小写."""
    e = ClassConfigEntry(alias="Person")
    assert e.alias == "person"
    e2 = ClassConfigEntry(alias="RIPE APPLE")
    assert e2.alias == "ripe apple"


def test_class_config_alias_strips_whitespace():
    e = ClassConfigEntry(alias="  apple  ")
    assert e.alias == "apple"


def test_class_config_alias_collapses_whitespace_runs():
    e = ClassConfigEntry(alias="ripe   apple")
    assert e.alias == "ripe apple"


def test_class_config_alias_collapses_multiple_commas():
    """逗号 + 周边空白折叠为单 ','."""
    e = ClassConfigEntry(alias="cat,,dog")
    assert e.alias == "cat,dog"
    e2 = ClassConfigEntry(alias="a, , b")
    assert e2.alias == "a,b"
    e3 = ClassConfigEntry(alias="a ,, b")
    assert e3.alias == "a,b"


def test_class_config_alias_strips_leading_trailing_commas():
    e = ClassConfigEntry(alias=",foo,")
    assert e.alias == "foo"


def test_class_config_alias_empty_string_to_none():
    e = ClassConfigEntry(alias="")
    assert e.alias is None
    e2 = ClassConfigEntry(alias="   ")
    assert e2.alias is None


def test_attribute_field_select_requires_options():
    with pytest.raises(ValidationError):
        AttributeSchema(
            fields=[
                AttributeField(key="x", label="X", type="select"),
            ]
        )


# ── Comment 子类型 ──────────────────────────────────────────────────


def test_mention_via_alias():
    m = Mention.model_validate(
        {
            "userId": "11111111-1111-1111-1111-111111111111",
            "displayName": "Alice",
            "offset": 0,
            "length": 5,
        }
    )
    assert str(m.user_id) == "11111111-1111-1111-1111-111111111111"


def test_attachment_prefix_enforced():
    Attachment.model_validate(
        {
            "storageKey": "comment-attachments/foo.png",
            "fileName": "foo.png",
            "mimeType": "image/png",
            "size": 100,
        }
    )
    with pytest.raises(ValidationError):
        Attachment.model_validate(
            {
                "storageKey": "../etc/passwd",
                "fileName": "x",
                "mimeType": "image/png",
                "size": 1,
            }
        )


def test_canvas_drawing_shapes_typed():
    cd = CanvasDrawing(
        shapes=[
            CanvasShape(type="line", points=[0.1, 0.2, 0.3, 0.4], stroke="#ef4444"),
        ]
    )
    assert cd.shapes[0].type == "line"
    assert cd.shapes[0].points == [0.1, 0.2, 0.3, 0.4]
    # 不允许 extra
    with pytest.raises(ValidationError):
        CanvasShape.model_validate({"type": "line", "points": [0, 0], "extra": 1})


# ── Audit detail：extra=allow + 已知字段类型化 ─────────────────────


def test_audit_detail_extra_allowed():
    d = AuditDetail.model_validate(
        {
            "request_id": "abc",
            "task_id": "tid",
            "field_key": "color",
            "before": "red",
            "after": "blue",
            "anything": {"nested": True},
        }
    )
    assert d.request_id == "abc"
    # extra 字段保留
    assert d.model_extra == {"anything": {"nested": True}}


# ── AnnotationOut legacy bbox auto-normalize ───────────────────────


def test_annotation_out_normalizes_legacy_bbox():
    from app.schemas.annotation import AnnotationOut
    from datetime import datetime, timezone
    from uuid import uuid4

    out = AnnotationOut.model_validate(
        {
            "id": uuid4(),
            "task_id": uuid4(),
            "source": "manual",
            "annotation_type": "bbox",
            "class_name": "car",
            # 历史数据缺 type
            "geometry": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
    )
    assert isinstance(out.geometry, BboxGeometry)
    assert out.geometry.type == "bbox"


# ── AnnotationAttributes 元素类型受限 ───────────────────────────────


def test_annotation_attributes_value_types():
    from pydantic import TypeAdapter

    AA = TypeAdapter(AnnotationAttributes)
    AA.validate_python({"k1": "str", "k2": 1, "k3": True, "k4": ["a"], "k5": None})
    # multiselect 列表必须 str
    AA.validate_python({"k": ["a", "b"]})


# ── v0.13.0 · 点云 3D 几何（box_3d / point_mask_3d）─────────────────


def test_box_3d_geometry_round_trip():
    g = Box3DGeometry(
        center=[1, 2, 3],
        size=[4, 5, 6],
        rotation=[0, 0, 0],
        convention_at_create="apollo",
    )
    assert g.type == "box_3d"
    dumped = g.model_dump()
    g2 = Box3DGeometry.model_validate(dumped)
    assert g2.center == [1, 2, 3]
    assert g2.size == [4, 5, 6]
    assert g2.rotation == [0, 0, 0]
    assert g2.convention_at_create == "apollo"
    assert g2.type == "box_3d"


def test_box_3d_geometry_length_enforced():
    # center/size/rotation 必须长度 3
    with pytest.raises(ValidationError):
        Box3DGeometry(center=[1, 2], size=[4, 5, 6], rotation=[0, 0, 0])
    with pytest.raises(ValidationError):
        Box3DGeometry(center=[1, 2, 3], size=[4, 5, 6, 7], rotation=[0, 0, 0])


def test_point_mask_geometry_round_trip():
    g = PointMaskGeometry(
        point_indices=[0, 1, 2],
        convention_at_create="sustechpoints_demo",
        decimate_stride=3,
        source_point_count=100,
    )
    assert g.type == "point_mask_3d"
    dumped = g.model_dump()
    g2 = PointMaskGeometry.model_validate(dumped)
    assert g2.point_indices == [0, 1, 2]
    assert g2.convention_at_create == "sustechpoints_demo"
    assert g2.decimate_stride == 3
    assert g2.source_point_count == 100
    assert g2.type == "point_mask_3d"
    # 默认空列表
    assert PointMaskGeometry().point_indices == []


def test_point_mask_geometry_rejects_negative():
    with pytest.raises(ValidationError):
        PointMaskGeometry(point_indices=[-1])
    with pytest.raises(ValidationError):
        PointMaskGeometry(point_indices=[1], convention_at_create="bad_axis")
    with pytest.raises(ValidationError):
        PointMaskGeometry(point_indices=[1], decimate_stride=0)


def test_geometry_union_dispatches_3d_types():
    from pydantic import TypeAdapter

    GA = TypeAdapter(Geometry)
    box = GA.validate_python(
        {
            "type": "box_3d",
            "center": [1, 2, 3],
            "size": [4, 5, 6],
            "rotation": [0, 0, 0],
        }
    )
    assert isinstance(box, Box3DGeometry)
    mask = GA.validate_python({"type": "point_mask_3d", "point_indices": [0, 1, 2]})
    assert isinstance(mask, PointMaskGeometry)


def test_geometry_union_still_dispatches_2d_types():
    """回归：旧 2D 类型（bbox）经判别联合仍正常解析，不受 3D 新增影响。"""
    from pydantic import TypeAdapter

    GA = TypeAdapter(Geometry)
    bbox = GA.validate_python({"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    assert isinstance(bbox, BboxGeometry)
    assert bbox.x == 0.1


# ── v0.13.1 · 相机标定 SensorCalibration / DatasetItemMetadata ───────


def test_sensor_calibration_round_trip():
    extrinsic = [float(i) for i in range(16)]
    intrinsic = [float(i) for i in range(9)]
    c = SensorCalibration(extrinsic=extrinsic, intrinsic=intrinsic)
    assert c.rect is None
    dumped = c.model_dump()
    c2 = SensorCalibration.model_validate(dumped)
    assert c2.extrinsic == extrinsic
    assert c2.intrinsic == intrinsic
    assert c2.rect is None


def test_sensor_calibration_round_trip_with_rect():
    extrinsic = [float(i) for i in range(16)]
    intrinsic = [float(i) for i in range(9)]
    rect = [float(i) for i in range(16)]
    c = SensorCalibration(extrinsic=extrinsic, intrinsic=intrinsic, rect=rect)
    dumped = c.model_dump()
    c2 = SensorCalibration.model_validate(dumped)
    assert c2.rect == rect


def test_sensor_calibration_rejects_bad_lengths():
    intrinsic = [float(i) for i in range(9)]
    extrinsic = [float(i) for i in range(16)]
    # extrinsic 长度 15
    with pytest.raises(ValidationError):
        SensorCalibration(extrinsic=[float(i) for i in range(15)], intrinsic=intrinsic)
    # intrinsic 长度 8
    with pytest.raises(ValidationError):
        SensorCalibration(extrinsic=extrinsic, intrinsic=[float(i) for i in range(8)])
    # rect 长度 10
    with pytest.raises(ValidationError):
        SensorCalibration(
            extrinsic=extrinsic,
            intrinsic=intrinsic,
            rect=[float(i) for i in range(10)],
        )


def test_dataset_item_metadata_calibration_typed_extra_preserved():
    m = DatasetItemMetadata.model_validate(
        {
            "calibration": {
                "extrinsic": [float(i) for i in range(16)],
                "intrinsic": [float(i) for i in range(9)],
            },
            "foo": 123,
        }
    )
    assert isinstance(m.calibration, SensorCalibration)
    # extra="allow" 保留未声明 key
    assert m.model_dump()["foo"] == 123


def test_dataset_item_metadata_empty_calibration_none():
    m = DatasetItemMetadata()
    assert m.calibration is None


# ── 退役 ai_interactive 工具单位 ─────────────────────────────────────


def test_map_retired_tool_unit_by_geometry():
    from aap_workspace.configuration_values import map_retired_tool_unit

    for gtype in (
        "polygon",
        "multi_polygon",
        "mask",
        "video_polygon",
        "video_track_polygon",
    ):
        assert map_retired_tool_unit(gtype) == "region"
    for gtype in ("bbox", "rotated_bbox", "video_bbox", None, "keypoint"):
        assert map_retired_tool_unit(gtype) == "bbox"


def test_annotation_create_remaps_retired_ai_interactive_polygon_to_region():
    from app.schemas.annotation import AnnotationCreate

    m = AnnotationCreate.model_validate(
        {
            "tool_unit_id": "ai_interactive",
            "class_name": "obj",
            "geometry": {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
        }
    )
    # 遗留值按几何归位, 不 422。
    assert m.tool_unit_id == "region"


def test_annotation_create_remaps_retired_ai_interactive_bbox_to_bbox():
    from app.schemas.annotation import AnnotationCreate

    m = AnnotationCreate.model_validate(
        {
            "tool_unit_id": "ai_interactive",
            "class_name": "obj",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        }
    )
    assert m.tool_unit_id == "bbox"


def test_tool_unit_id_literal_rejects_ai_interactive_on_response_models():
    from app.schemas.annotation import AnnotationOut

    # AnnotationOut (响应模型) 不套入口映射: 退役值直接被 Literal 拒绝。
    with pytest.raises(ValidationError):
        AnnotationOut.model_validate(
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "task_id": "00000000-0000-0000-0000-000000000002",
                "source": "manual",
                "annotation_type": "bbox",
                "tool_unit_id": "ai_interactive",
                "class_name": "obj",
                "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
                "is_active": True,
                "created_at": "2026-01-01T00:00:00Z",
            }
        )


def test_derive_tool_unit_ignores_retired_ai_interactive():
    from app.services.prediction import derive_tool_unit_from_result

    # result item 显式带退役值时, 按几何 type 派生, 不原样返回 'ai_interactive'。
    assert (
        derive_tool_unit_from_result(
            [{"tool_unit_id": "ai_interactive", "type": "polygonlabels"}]
        )
        == "region"
    )
    assert (
        derive_tool_unit_from_result(
            [{"tool_unit_id": "ai_interactive", "type": "rectanglelabels"}]
        )
        == "bbox"
    )
