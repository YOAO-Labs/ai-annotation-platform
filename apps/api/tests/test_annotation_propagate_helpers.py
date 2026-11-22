"""annotation 视频轨迹传播纯函数簇表征测试。

守护 services/annotation.py 的模块级纯函数 _new_track_id / _clean_bbox_geometry /
_composition_keyframe / _track_visible_keyframes / _clip_outside_ranges —— 无 DB 依赖,
可直接喂 dict 断言输出。作为「巨石拆分 Epic」缓拆项补测试再拆的守护网:本文件先对
当前代码绿,后续把这簇搬到 annotation_propagation.py 后(annotation.py re-export 自用),
本文件 import 一字不改仍绿。
"""

from __future__ import annotations

import uuid

from app.services.annotation import (
    _clean_bbox_geometry,
    _clip_outside_ranges,
    _composition_keyframe,
    _new_track_id,
    _track_visible_keyframes,
)


class TestNewTrackId:
    def test_prefix_and_valid_uuid(self):
        tid = _new_track_id()
        assert tid.startswith("trk_")
        uuid.UUID(tid.removeprefix("trk_"))

    def test_unique_each_call(self):
        assert _new_track_id() != _new_track_id()


class TestCleanBboxGeometry:
    def test_missing_keys_default_zero(self):
        assert _clean_bbox_geometry({}) == {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}

    def test_string_values_coerced(self):
        assert _clean_bbox_geometry({"x": "1.5", "y": "2", "w": "0", "h": "3.25"}) == {
            "x": 1.5,
            "y": 2.0,
            "w": 0.0,
            "h": 3.25,
        }

    def test_extra_keys_dropped(self):
        out = _clean_bbox_geometry({"x": 1, "y": 1, "w": 1, "h": 1, "rotation": 9})
        assert set(out.keys()) == {"x", "y", "w", "h"}


class TestCompositionKeyframe:
    def test_manual_source_default(self):
        assert _composition_keyframe(3, {"x": 1, "y": 2, "w": 3, "h": 4}) == {
            "frame_index": 3,
            "bbox": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
            "source": "manual",
            "occluded": False,
        }

    def test_prediction_source_preserved(self):
        assert (
            _composition_keyframe(0, {}, source="prediction")["source"] == "prediction"
        )

    def test_unknown_source_falls_back_to_manual(self):
        assert _composition_keyframe(0, {}, source="whatever")["source"] == "manual"


class TestTrackVisibleKeyframes:
    def test_filters_keyframes_inside_outside_range(self):
        geom = {
            "keyframes": [
                {"frame_index": 1, "bbox": {}},
                {"frame_index": 5, "bbox": {}},
            ],
            "outside": [{"from": 4, "to": 6}],
        }
        assert [kf["frame_index"] for kf in _track_visible_keyframes(geom)] == [1]

    def test_all_visible_and_sorted_when_no_outside(self):
        geom = {"keyframes": [{"frame_index": 2}, {"frame_index": 1}]}
        assert [kf["frame_index"] for kf in _track_visible_keyframes(geom)] == [1, 2]

    def test_empty_when_no_keyframes(self):
        assert _track_visible_keyframes({}) == []


class TestClipOutsideRanges:
    def test_clips_to_start_and_end(self):
        geom = {"outside": [{"from": 0, "to": 10}]}
        assert _clip_outside_ranges(geom, start=3, end=7) == [
            {"from": 3, "to": 7, "source": "manual"}
        ]

    def test_start_only_keeps_upper_bound(self):
        geom = {"outside": [{"from": 0, "to": 10}]}
        assert _clip_outside_ranges(geom, start=5, end=None) == [
            {"from": 5, "to": 10, "source": "manual"}
        ]

    def test_inverted_after_clip_is_dropped(self):
        geom = {"outside": [{"from": 0, "to": 2}]}
        assert _clip_outside_ranges(geom, start=5, end=None) == []

    def test_prediction_source_preserved(self):
        geom = {"outside": [{"from": 0, "to": 10, "source": "prediction"}]}
        assert _clip_outside_ranges(geom, start=None, end=None) == [
            {"from": 0, "to": 10, "source": "prediction"}
        ]

    def test_empty_when_no_outside(self):
        assert _clip_outside_ranges({}, start=None, end=None) == []
