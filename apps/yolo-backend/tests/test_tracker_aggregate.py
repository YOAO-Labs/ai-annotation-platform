"""检测式视频追踪聚合逻辑单测 (无 GPU/无 ultralytics).

覆盖 predictor 的 _accumulate_track_frame + _emit_tracks: 逐帧带 id 检测框按 track_id
聚合、坐标归一 0-1、class 多数票、score 均值、keyframe source=prediction。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_heavy_imports() -> None:
    """predictor 顶层 import ultralytics/torch 间接依赖; 桩掉让纯逻辑可导入。"""
    sys.modules.setdefault("torch", MagicMock())
    sys.modules.setdefault("ultralytics", MagicMock())


class _IdTensor:
    """模拟 ultralytics boxes.id 的 .int().cpu().tolist() 链。"""

    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    def int(self) -> "_IdTensor":
        return self

    def cpu(self) -> "_IdTensor":
        return self

    def tolist(self) -> list[int]:
        return self._ids


def _fake_frame(ids, xyxy, confs, clss, names, wh=(100, 200)):
    """构造一个最小 Results 替身。orig_shape=(h,w)。"""
    w, h = wh
    boxes = types.SimpleNamespace(
        id=_IdTensor(ids) if ids is not None else None,
        xyxy=np.array(xyxy, dtype=float),
        conf=np.array(confs, dtype=float),
        cls=list(clss),
    )
    return types.SimpleNamespace(boxes=boxes, orig_shape=(h, w), names=names)


def test_accumulate_and_emit_groups_by_track_id() -> None:
    from predictor import _accumulate_track_frame, _emit_tracks

    names = {0: "car", 1: "person"}
    tracks: dict = {}
    # w=100,h=200。track 3 出现在帧0/1, track 7 仅帧0。
    _accumulate_track_frame(
        _fake_frame(
            [3, 7], [[10, 20, 50, 80], [0, 0, 100, 200]], [0.9, 0.5], [0, 1], names
        ),
        0,
        tracks,
    )
    _accumulate_track_frame(
        _fake_frame([3], [[12, 22, 52, 82]], [0.8], [0], names),
        1,
        tracks,
    )
    items = _emit_tracks(tracks)

    assert [it["track_id"] for it in items] == [3, 7]  # 升序稳定
    trk3 = items[0]
    assert trk3["type"] == "video_track_bbox"
    assert trk3["class_name"] == "car"
    assert trk3["score"] == pytest.approx((0.9 + 0.8) / 2)
    assert len(trk3["keyframes"]) == 2
    # 帧0: x=10/100=0.1, y=20/200=0.1, w=40/100=0.4, h=60/200=0.3
    assert trk3["keyframes"][0] == {
        "frame_index": 0,
        "bbox": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.3},
        "score": pytest.approx(0.9),
    }
    # 全图框 track 7: 归一到 (0,0,1,1)。
    assert items[1]["keyframes"][0]["bbox"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def test_frame_without_track_id_skipped() -> None:
    from predictor import _accumulate_track_frame

    tracks: dict = {}
    _accumulate_track_frame(_fake_frame(None, [], [], [], {}), 0, tracks)
    assert tracks == {}


def test_class_majority_vote_across_frames() -> None:
    from predictor import _accumulate_track_frame, _emit_tracks

    names = {0: "car", 1: "truck"}
    tracks: dict = {}
    # track 1 三帧: car, car, truck → 多数票 car。
    for f, c in enumerate([0, 0, 1]):
        _accumulate_track_frame(
            _fake_frame([1], [[0, 0, 10, 10]], [0.5], [c], names), f, tracks
        )
    assert _emit_tracks(tracks)[0]["class_name"] == "car"


def test_coords_clamped_to_unit_range() -> None:
    from predictor import _accumulate_track_frame, _emit_tracks

    tracks: dict = {}
    # 越界框 (亚像素负值 / 超出图幅) 应被 clamp 到 [0,1]。
    _accumulate_track_frame(
        _fake_frame([1], [[-5, -5, 120, 260]], [0.7], [0], {0: "car"}, wh=(100, 200)),
        0,
        tracks,
    )
    bbox = _emit_tracks(tracks)[0]["keyframes"][0]["bbox"]
    assert bbox["x"] == 0.0 and bbox["y"] == 0.0
    assert bbox["w"] == 1.0 and bbox["h"] == 1.0
