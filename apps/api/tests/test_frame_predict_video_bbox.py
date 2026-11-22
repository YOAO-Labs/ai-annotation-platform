"""视频单题 AI: 图像 backend 检测结果 → 单帧 video_bbox 转换 (to_video_bbox_result)."""

from __future__ import annotations

from app.services.prediction import to_video_bbox_result


def test_rectanglelabels_0_1_to_video_bbox():
    raw = [
        {
            "type": "rectanglelabels",
            "score": 0.9,
            "value": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "rectanglelabels": ["car"],
            },
        }
    ]
    out = to_video_bbox_result(raw, frame_index=12)
    assert len(out) == 1
    item = out[0]
    assert item["type"] == "video_bbox"
    assert item["class_name"] == "car"
    assert item["confidence"] == 0.9
    assert item["geometry"] == {
        "type": "video_bbox",
        "frame_index": 12,
        "x": 0.1,
        "y": 0.2,
        "w": 0.3,
        "h": 0.4,
    }


def test_percent_coords_normalized_to_0_1():
    raw = [
        {
            "type": "rectanglelabels",
            "score": 0.8,
            "value": {
                "x": 10,
                "y": 20,
                "width": 30,
                "height": 40,
                "rectanglelabels": ["person"],
            },
        }
    ]
    out = to_video_bbox_result(raw, frame_index=0)
    geom = out[0]["geometry"]
    assert geom["x"] == 0.1
    assert geom["y"] == 0.2
    assert geom["w"] == 0.3
    assert geom["h"] == 0.4


def test_non_bbox_shapes_skipped():
    raw = [
        {
            "type": "polygonlabels",
            "score": 0.7,
            "value": {
                "points": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
                "polygonlabels": ["road"],
            },
        },
        {
            "type": "rectanglelabels",
            "score": 0.6,
            "value": {
                "x": 0.5,
                "y": 0.5,
                "width": 0.1,
                "height": 0.1,
                "rectanglelabels": ["car"],
            },
        },
    ]
    out = to_video_bbox_result(raw, frame_index=3)
    assert len(out) == 1
    assert out[0]["class_name"] == "car"
    assert out[0]["geometry"]["frame_index"] == 3


def test_frame_index_stamped_on_every_box():
    raw = [
        {
            "type": "rectanglelabels",
            "score": 0.5,
            "value": {
                "x": 0.0,
                "y": 0.0,
                "width": 0.1,
                "height": 0.1,
                "rectanglelabels": ["a"],
            },
        },
        {
            "type": "rectanglelabels",
            "score": 0.5,
            "value": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.1,
                "height": 0.1,
                "rectanglelabels": ["b"],
            },
        },
    ]
    out = to_video_bbox_result(raw, frame_index=7)
    assert [o["geometry"]["frame_index"] for o in out] == [7, 7]


def test_empty_and_non_dict_input():
    assert to_video_bbox_result([], frame_index=0) == []
    assert to_video_bbox_result(["garbage", None], frame_index=0) == []  # type: ignore[list-item]
