"""视频导出纯函数单测（无 DB）。

覆盖采样网格映射（D2 重编号）+ MOT/KITTI 文本生成 + outside 省略。
"""

from __future__ import annotations

import json

from app.services.mask_formats.image_codecs import normalize_coco_segmentation_rle
from app.services.exporting.video import (
    VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES,
    VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES,
    build_coco_frames_seg,
    build_yolo_frame_det_labels,
    build_kitti_labels,
    build_mot_gt,
    build_mot_seqinfo,
    effective_fps,
    points_to_bbox_norm,
    source_to_grid,
    track_grid_rows,
)
from app.services.exporting.service import _clean_video_single_frame_geometry


def _polygon_track(
    track_id: str, keyframes: list[tuple[int, list[list[float]]]], outside=None
) -> dict:
    return {
        "type": "video_track_polygon",
        "track_id": track_id,
        "keyframes": [
            {"frame_index": f, "points": pts, "source": "manual"}
            for (f, pts) in keyframes
        ],
        "outside": outside or [],
    }


def _track(
    track_id: str, keyframes: list[tuple[int, float, float, float, float]], outside=None
) -> dict:
    return {
        "type": "video_track_bbox",
        "track_id": track_id,
        "keyframes": [
            {
                "frame_index": f,
                "bbox": {"x": x, "y": y, "w": w, "h": h},
                "source": "manual",
            }
            for (f, x, y, w, h) in keyframes
        ],
        "outside": outside or [],
    }


def test_effective_fps_divides_by_step():
    assert effective_fps(60, 6) == 10.0
    assert effective_fps(30, 1) == 30.0
    assert effective_fps(None, 6) is None


def test_source_to_grid_renumbers_on_grid():
    # frame_count=13, step=6 → 网格源帧 [0,6,12] → 序号 {0:0,6:1,12:2}
    assert source_to_grid(13, 6) == {0: 0, 6: 1, 12: 2}


def test_selected_source_frames_keep_global_sampling_phase_and_dense_output_ids():
    geometry = _track("a", [(0, 0.0, 0.0, 0.5, 0.5), (12, 0.6, 0.6, 0.5, 0.5)])
    selected = [6, 12]

    assert source_to_grid(13, 6, selected) == {6: 0, 12: 1}
    mot = build_mot_gt(
        [(1, "car", geometry)],
        frame_count=13,
        step=6,
        img_w=100,
        img_h=100,
        source_frames=selected,
    )
    assert [line.split(",", 1)[0] for line in mot.splitlines()] == ["1", "2"]
    seqinfo = build_mot_seqinfo(
        "selected",
        source_fps=60,
        step=6,
        frame_count=13,
        img_w=100,
        img_h=100,
        source_frames=selected,
    )
    assert "seqLength=2" in seqinfo


def test_video_json_preserves_obb_and_keypoint_geometry_losslessly():
    obb = {
        "type": "video_rotated_bbox",
        "frame_index": 7,
        "cx": 0.5,
        "cy": 0.4,
        "w": 0.3,
        "h": 0.2,
        "angle": 37.5,
    }
    keypoint = {
        "type": "video_keypoint",
        "frame_index": 8,
        "points": [
            {"x": 0.1, "y": 0.2, "v": 2},
            {"x": 0.3, "y": 0.4, "v": 0},
        ],
    }
    assert _clean_video_single_frame_geometry(obb) == obb
    assert _clean_video_single_frame_geometry(keypoint) == keypoint
    assert {"video_rotated_bbox", "video_keypoint"} <= (
        VIDEO_LOSSLESS_SINGLE_FRAME_GEOMETRY_TYPES
    )
    assert not {"video_rotated_bbox", "video_keypoint"} & (
        VIDEO_BBOX_COMPATIBLE_SINGLE_FRAME_GEOMETRY_TYPES
    )


def test_track_grid_rows_only_keeps_grid_frames():
    # step=6: 源帧 0/6/12 在网格上, 中间插值帧被丢弃。
    geom = _track("a", [(0, 0.0, 0.0, 0.5, 0.5), (12, 0.0, 0.0, 0.5, 0.5)])
    rows = track_grid_rows(geom, frame_count=13, step=6, img_w=1000, img_h=1000)
    assert [r["grid_index"] for r in rows] == [0, 1, 2]
    # 0..1 区间整段 bbox 不变 → 像素 left/top=0, w/h=500。
    assert rows[0]["left"] == 0 and rows[0]["w"] == 500.0


def test_outside_frames_dropped():
    geom = _track(
        "a",
        [(0, 0.0, 0.0, 0.5, 0.5), (12, 0.0, 0.0, 0.5, 0.5)],
        outside=[{"from": 6, "to": 6, "source": "manual"}],
    )
    rows = track_grid_rows(geom, frame_count=13, step=6, img_w=1000, img_h=1000)
    # 网格帧 6 落在 outside → 省略, 只剩 0 和 12 (grid 0,2)。
    assert [r["grid_index"] for r in rows] == [0, 2]


def test_mot_gt_format_and_1based_frame():
    geom = _track("a", [(0, 0.1, 0.2, 0.3, 0.4)])
    out = build_mot_gt(
        [(1, "car", geom)], frame_count=1, step=1, img_w=1000, img_h=1000
    )
    # frame 1-based, id=1, bbox px = 100,200,300,400, conf=1, x/y/z=-1
    assert out == "1,1,100.0,200.0,300.0,400.0,1,-1,-1,-1"


def test_mot_seqinfo_framerate_uses_sampled_fps():
    info = build_mot_seqinfo(
        "seq01", source_fps=60, step=6, frame_count=13, img_w=1920, img_h=1080
    )
    assert "frameRate=10" in info
    assert "seqLength=3" in info  # 网格帧 [0,6,12]
    assert "imWidth=1920" in info


def test_kitti_labels_0based_frame_and_occluded():
    geom = _track("a", [(0, 0.0, 0.0, 0.5, 0.5)])
    geom["keyframes"][0]["occluded"] = True
    out = build_kitti_labels(
        [(2, "Pedestrian", geom)], frame_count=1, step=1, img_w=1000, img_h=1000
    )
    parts = out.split(" ")
    assert parts[0] == "0"  # frame 0-based
    assert parts[1] == "2"  # track id
    assert parts[2] == "Pedestrian"
    assert parts[4] == "1"  # occluded


def test_yolo_frame_det_labels_merge_bbox_and_track_on_grid():
    track = _track(
        "a",
        [(0, 0.0, 0.0, 0.2, 0.2), (12, 0.6, 0.6, 0.2, 0.2)],
        outside=[{"from": 6, "to": 6, "source": "manual"}],
    )
    labels = build_yolo_frame_det_labels(
        tracks=[("car", track, {"track_attr": True})],
        bboxes=[
            (
                "person",
                {
                    "type": "video_bbox",
                    "frame_index": 6,
                    "x": 0.2,
                    "y": 0.2,
                    "w": 0.2,
                    "h": 0.4,
                },
                {"bbox_attr": True},
            )
        ],
        cat_map={"car": 0, "person": 1},
        frame_count=13,
        step=6,
        frame_start_number=1,
        include_attributes=True,
    )

    assert sorted(labels.keys()) == [1, 2, 3]
    assert labels[1][0] == ["0 0.100000 0.100000 0.200000 0.200000"]
    # Track frame 6 is outside, while video_bbox on the same grid frame remains.
    assert labels[2][0] == ["1 0.300000 0.400000 0.200000 0.400000"]
    assert labels[2][1] == [{"bbox_attr": True}]
    assert labels[3][0] == ["0 0.700000 0.700000 0.200000 0.200000"]


def test_yolo_frame_det_skips_off_grid_bboxes_and_keeps_empty_labels():
    labels = build_yolo_frame_det_labels(
        tracks=[],
        bboxes=[
            (
                "person",
                {
                    "type": "video_bbox",
                    "frame_index": 3,
                    "x": 0.2,
                    "y": 0.2,
                    "w": 0.2,
                    "h": 0.4,
                },
                {},
            ),
            (
                "person",
                {
                    "type": "video_bbox",
                    "frame_index": 4,
                    "x": 0.8,
                    "y": 0.8,
                    "w": 0.1,
                    "h": 0.1,
                },
                {},
            ),
        ],
        cat_map={"person": 0},
        frame_count=7,
        step=3,
        frame_start_number=1,
        include_attributes=False,
    )

    assert sorted(labels.keys()) == [1, 2, 3]
    assert labels[1][0] == []
    assert labels[2][0] == ["0 0.300000 0.400000 0.200000 0.400000"]
    assert labels[3][0] == []


# ── polygon/polyline track → bbox-only 格式降级为顶点外接框 ──


def test_points_to_bbox_norm_computes_bounding_box():
    # 三角形顶点 → 外接框 x=0.1,y=0.2,w=0.4,h=0.3。
    pts = [[0.1, 0.2], [0.5, 0.2], [0.3, 0.5]]
    assert points_to_bbox_norm(pts) == {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3}


def test_points_to_bbox_norm_empty_is_zero():
    assert points_to_bbox_norm([]) == {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


def test_polygon_track_grid_rows_use_vertex_bounds_not_zero():
    # polygon track 关键帧存 points；MOT/KITTI 走 track_grid_rows → 顶点外接框像素。
    geom = _polygon_track("a", [(0, [[0.1, 0.2], [0.5, 0.2], [0.3, 0.6]])])
    rows = track_grid_rows(geom, frame_count=1, step=1, img_w=1000, img_h=1000)
    assert len(rows) == 1
    # 外接框归一 x=0.1,y=0.2,w=0.4,h=0.4 → 像素 100,200,400,400（非全 0）。
    assert (rows[0]["left"], rows[0]["top"], rows[0]["w"], rows[0]["h"]) == (
        100.0,
        200.0,
        400.0,
        400.0,
    )


def test_polygon_track_mot_and_kitti_emit_bounding_box():
    geom = _polygon_track("a", [(0, [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]])])
    mot = build_mot_gt(
        [(1, "car", geom)], frame_count=1, step=1, img_w=1000, img_h=1000
    )
    # 正方形外接框 = 自身 → px 0,0,500,500。
    assert mot == "1,1,0.0,0.0,500.0,500.0,1,-1,-1,-1"
    kitti = build_kitti_labels(
        [(1, "Car", geom)], frame_count=1, step=1, img_w=1000, img_h=1000
    )
    parts = kitti.split(" ")
    # x1 y1 x2 y2 = 0 0 500 500。
    assert parts[6:10] == ["0.0", "0.0", "500.0", "500.0"]


def test_polygon_track_yolo_det_emits_bounding_box_center():
    geom = _polygon_track("a", [(0, [[0.0, 0.0], [0.4, 0.0], [0.4, 0.4], [0.0, 0.4]])])
    labels = build_yolo_frame_det_labels(
        tracks=[("car", geom, {})],
        bboxes=[],
        cat_map={"car": 0},
        frame_count=1,
        step=1,
        frame_start_number=1,
        include_attributes=False,
    )
    # 外接框 x0=0,y0=0,w=0.4,h=0.4 → YOLO cx=cy=0.2, bw=bh=0.4。
    assert labels[1][0] == ["0 0.200000 0.200000 0.400000 0.400000"]


# ── COCO frame segmentation（纯函数）──────────────────────────────────


def _single_polygon(frame_index: int, points: list[list[float]]) -> dict:
    return {"type": "video_polygon", "frame_index": frame_index, "points": points}


def test_coco_frames_seg_builds_images_and_annotations():
    """单帧 polygon + polygon track：像素 segmentation / 外接框 bbox / 稳定 id / 空帧 image。"""
    track = _polygon_track(
        "trk",
        [
            (0, [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2]]),
            (4, [[0.6, 0.6], [0.8, 0.6], [0.8, 0.8]]),
        ],
        outside=[{"from": 2, "to": 2}],
    )
    single = _single_polygon(2, [[0.2, 0.2], [0.4, 0.2], [0.4, 0.6], [0.2, 0.6]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "clip-a",
                "tracks": [("car", track, {"speed": 50}, None)],
                "bboxes": [("person", single, {"speed": 3}, None)],
                "frame_count": 5,
                "step": 2,
                "img_w": 640,
                "img_h": 360,
            }
        ],
        {"car": 0, "person": 1},
        frame_start_number=1,
        include_attributes=True,
        description="Video Polygon Project",
    )

    # 采样网格 [0,2,4] → 3 image（含无标注空帧），1-based 文件名 + 源帧号。
    assert [im["file_name"] for im in doc["images"]] == [
        "images/clip-a/000001.jpg",
        "images/clip-a/000002.jpg",
        "images/clip-a/000003.jpg",
    ]
    assert [im["source_frame_index"] for im in doc["images"]] == [0, 2, 4]
    assert all(im["width"] == 640 and im["height"] == 360 for im in doc["images"])

    anns = doc["annotations"]
    # bboxes 先（person@frame2），tracks 后（car@frame0 / car@frame4，outside frame2 省略）。
    assert len(anns) == 3
    person = anns[0]
    assert person["category_id"] == 1
    assert person["image_id"] == 1  # frame_no 2
    assert person["segmentation"] == [
        [128.0, 72.0, 256.0, 72.0, 256.0, 216.0, 128.0, 216.0]
    ]
    assert person["bbox"] == [128.0, 72.0, 128.0, 144.0]
    assert person["area"] == 18432.0
    assert person["iscrowd"] == 0
    assert person["attributes"] == {"speed": 3}
    assert anns[1]["category_id"] == 0 and anns[1]["image_id"] == 0  # car@frame0
    assert anns[2]["image_id"] == 2  # car@frame4

    assert doc["categories"] == [
        {"id": 0, "name": "car"},
        {"id": 1, "name": "person"},
    ]
    # 结构契约：image_id / category_id 引用完整（等价 pycocotools createIndex 可成功）。
    img_ids = {im["id"] for im in doc["images"]}
    cat_ids = {c["id"] for c in doc["categories"]}
    for a in anns:
        assert a["image_id"] in img_ids
        assert a["category_id"] in cat_ids


def test_coco_frames_seg_empty_frames_keep_image_records():
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [],
                "frame_count": 5,
                "step": 2,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=False,
    )
    assert len(doc["images"]) == 3
    assert doc["annotations"] == []


def test_coco_frames_seg_exports_mask_track_as_rle_crowd():
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [2, 2, 2]}
    geometry = {
        "type": "video_track_mask",
        "track_id": "mask-1",
        "keyframes": [
            {
                "frame_index": 0,
                "mask": {"encoding": "coco_rle_ref"},
                "mask_rle": rle,
                "bbox": {"x": 1 / 3, "y": 0, "w": 1 / 3, "h": 1},
                "source": "manual",
            }
        ],
        "outside": [],
    }
    doc = build_coco_frames_seg(
        [
            {
                "seq": "mask-seq",
                "tracks": [("car", geometry, {}, "mask-1")],
                "bboxes": [],
                "frame_count": 1,
                "step": 1,
                "img_w": 3,
                "img_h": 2,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    annotation = doc["annotations"][0]
    assert isinstance(annotation["segmentation"]["counts"], str)
    assert (
        normalize_coco_segmentation_rle(
            annotation["segmentation"], expected_width=3, expected_height=2
        )
        == rle
    )
    assert annotation["bbox"] == [1.0, 0.0, 1.0, 2.0]
    assert annotation["area"] == 2
    assert annotation["iscrowd"] == 1
    assert annotation["attributes"]["__track_id"] == "mask-1"
    assert annotation["attributes"]["__occluded"] is False


def test_coco_frames_seg_skips_bbox_and_polyline():
    bbox = {
        "type": "video_bbox",
        "frame_index": 0,
        "x": 0.1,
        "y": 0.1,
        "w": 0.2,
        "h": 0.2,
    }
    polyline = {
        "type": "video_polyline",
        "frame_index": 0,
        "points": [[0.1, 0.1], [0.5, 0.3], [0.3, 0.5]],
    }
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", bbox, {}, None), ("car", polyline, {}, None)],
                "frame_count": 5,
                "step": 2,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    assert doc["annotations"] == []


def test_coco_frames_seg_skips_off_grid_single_frame():
    single = _single_polygon(1, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", single, {}, None)],
                "frame_count": 5,
                "step": 2,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    # frame1 不在网格 [0,2,4] → 无 annotation，但仍有 3 张 image。
    assert doc["annotations"] == []
    assert len(doc["images"]) == 3


def test_coco_frames_seg_track_id_written_to_attributes():
    single = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", single, {"speed": 5}, "trk-7")],
                "frame_count": 3,
                "step": 1,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    assert doc["annotations"][0]["attributes"] == {"speed": 5, "__track_id": "trk-7"}


def test_coco_frames_seg_omits_attributes_when_disabled():
    single = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", single, {"speed": 5}, "trk-7")],
                "frame_count": 3,
                "step": 1,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=False,
    )
    assert "attributes" not in doc["annotations"][0]


def test_coco_frames_seg_unknown_class_is_skipped_not_zeroed():
    """未知/已删除类名的 annotation 整条跳过，不再静默落到 category_id=0（旧类撞车 footgun）。"""
    known = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    unknown = _single_polygon(0, [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", known, {}, None), ("ghost", unknown, {}, None)],
                "frame_count": 3,
                "step": 1,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    # 已知类正常导出；未知类（比如项目已删除的类）不落 category_id=0，直接跳过。
    assert len(doc["annotations"]) == 1
    assert doc["annotations"][0]["category_id"] == 0
    assert doc["info"]["skipped_unknown_class_annotations"] == 1
    assert doc["info"]["skipped_unknown_class_names"] == ["ghost"]

    from pycocotools.coco import COCO

    coco = COCO()
    coco.dataset = doc  # type: ignore[attr-defined]
    coco.createIndex()  # 引用不完整会在此抛 KeyError
    assert len(coco.getAnnIds()) == 1


def test_coco_frames_seg_none_class_name_is_skipped():
    single = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [(None, single, {}, None)],
                "frame_count": 3,
                "step": 1,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    assert doc["annotations"] == []
    assert doc["info"]["skipped_unknown_class_annotations"] == 1
    assert doc["info"]["skipped_unknown_class_names"] == ["(empty)"]


def test_coco_frames_seg_unknown_class_track_is_skipped():
    """track polygon 同款：整条 track 用未知类名时跳过所有帧,不落 category_id=0。"""
    track = _polygon_track(
        "trk",
        [
            (0, [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2]]),
            (4, [[0.6, 0.6], [0.8, 0.6], [0.8, 0.8]]),
        ],
    )
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [("ghost", track, {}, None)],
                "bboxes": [],
                "frame_count": 5,
                "step": 2,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {"car": 0},
        frame_start_number=1,
        include_attributes=True,
    )
    assert doc["annotations"] == []
    assert doc["info"]["skipped_unknown_class_annotations"] == 1
    assert doc["info"]["skipped_unknown_class_names"] == ["ghost"]


def test_coco_frames_seg_empty_classes_list_produces_loadable_coco():
    """classes_list 为空（cat_map={}）时 polygon 标注全部跳过，不产生指向不存在
    category 的悬空 category_id=0——此前的组合会让 pycocotools ``createIndex`` KeyError。
    """
    single = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "c",
                "tracks": [],
                "bboxes": [("car", single, {}, None)],
                "frame_count": 3,
                "step": 1,
                "img_w": 100,
                "img_h": 100,
            }
        ],
        {},
        frame_start_number=1,
        include_attributes=True,
    )
    assert doc["categories"] == []
    assert doc["annotations"] == []
    assert doc["info"]["skipped_unknown_class_annotations"] == 1
    assert doc["info"]["skipped_unknown_class_names"] == ["car"]

    from pycocotools.coco import COCO

    coco = COCO()
    coco.dataset = doc  # type: ignore[attr-defined]
    coco.createIndex()  # 此前 categories=[] + category_id=0 组合会在此 KeyError


def test_coco_frames_seg_deterministic_and_unique_ids_across_sequences():
    single_a = _single_polygon(0, [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]])
    single_b = _single_polygon(0, [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4]])
    seqs = [
        {
            "seq": "a",
            "tracks": [],
            "bboxes": [("car", single_a, {}, None)],
            "frame_count": 3,
            "step": 1,
            "img_w": 100,
            "img_h": 100,
        },
        {
            "seq": "b",
            "tracks": [],
            "bboxes": [("car", single_b, {}, None)],
            "frame_count": 3,
            "step": 2,
            "img_w": 100,
            "img_h": 100,
        },
    ]
    doc1 = build_coco_frames_seg(
        seqs, {"car": 0}, frame_start_number=1, include_attributes=True
    )
    doc2 = build_coco_frames_seg(
        seqs, {"car": 0}, frame_start_number=1, include_attributes=True
    )
    # 相同输入 → 字节稳定（cache 友好 + 下游 diff 稳定）。
    assert json.dumps(doc1) == json.dumps(doc2)
    # image / annotation id 从 0 连续自增、全局唯一。
    assert [im["id"] for im in doc1["images"]] == list(range(len(doc1["images"])))
    assert [a["id"] for a in doc1["annotations"]] == list(
        range(len(doc1["annotations"]))
    )
    # 两 sequence 同名叶子帧靠 seq 前缀区分，不冲突。
    names = {im["file_name"] for im in doc1["images"]}
    assert "images/a/000001.jpg" in names and "images/b/000001.jpg" in names


def test_coco_frames_seg_loads_with_pycocotools_and_decodes_mask():
    """真实消费方验证：官方 pycocotools 能加载产物并把 segmentation 解码成掩码。

    比结构契约更强——``createIndex`` 建索引成功证明引用完整，``annToMask`` 用官方
    ``maskUtils.frPyObjects`` 解码多边形，证明我们写的 flat-ring segmentation 是合法 COCO。
    """
    from pycocotools.coco import COCO

    track = _polygon_track(
        "trk",
        [
            (0, [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2]]),
            (4, [[0.6, 0.6], [0.8, 0.6], [0.8, 0.8]]),
        ],
        outside=[{"from": 2, "to": 2}],
    )
    single = _single_polygon(2, [[0.2, 0.2], [0.4, 0.2], [0.4, 0.6], [0.2, 0.6]])
    doc = build_coco_frames_seg(
        [
            {
                "seq": "clip-a",
                "tracks": [("car", track, {"speed": 50}, None)],
                "bboxes": [("person", single, {"speed": 3}, None)],
                "frame_count": 5,
                "step": 2,
                "img_w": 640,
                "img_h": 360,
            }
        ],
        {"car": 0, "person": 1},
        frame_start_number=1,
        include_attributes=True,
    )

    coco = COCO()
    coco.dataset = doc  # type: ignore[attr-defined]  # 官方 in-memory 加载路径
    coco.createIndex()  # 引用不完整会在此抛 KeyError

    ann_ids = coco.getAnnIds()
    assert len(ann_ids) == 3
    assert set(coco.getCatIds()) == {0, 1}

    # 每条 annotation 的 segmentation 都能被官方 annToMask 解码成该帧尺寸的非空掩码。
    for ann in coco.loadAnns(ann_ids):
        mask = coco.annToMask(ann)
        assert mask.shape == (360, 640)
        assert int(mask.sum()) > 0

    # person 是像素 [128,72]–[256,216] 的方形多边形，掩码面积应落在其外接框内且非空。
    person = next(a for a in coco.loadAnns(ann_ids) if a["category_id"] == 1)
    assert 0 < int(coco.annToMask(person).sum()) <= 128 * 144
