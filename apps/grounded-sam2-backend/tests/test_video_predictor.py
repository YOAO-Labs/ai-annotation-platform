"""SAM2VideoTracker 单测 (无 GPU / fake predictor).

覆盖:
  - 坐标归一化: 归一化 seed bbox → 像素 xyxy; mask 外接框 → 归一化 {x,y,w,h}。
  - outside 判定: 空 mask → outside=True 零框; 非空 → outside=False。
  - 窗内传播 (多目标): fake predictor 逐对象 yield [num_obj,1,H,W],
    验证源帧号映射 / direction / seed 帧锚定 / 逐对象 instance_id / 点种子透传 / 多帧 prompt。
  - A1 真实 object score 作 confidence。

不构建真实模型: 用 SAM2VideoTracker.__new__ 绕过 __init__ 的 _load_video_predictor。
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from video_predictor import SAM2VideoTracker


# ---------- 坐标归一化 (静态方法, 不需实例) ----------


def test_seed_to_pixel_xyxy_basic():
    box = SAM2VideoTracker._seed_to_pixel_xyxy(
        {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}, w=200, h=100
    )
    assert box == pytest.approx([20.0, 20.0, 120.0, 60.0])


def test_seed_to_pixel_xyxy_accepts_width_height_aliases():
    box = SAM2VideoTracker._seed_to_pixel_xyxy(
        {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}, w=640, h=480
    )
    assert box == pytest.approx([0.0, 0.0, 640.0, 480.0])


def test_mask_to_bbox_norm_nonempty():
    mask = np.zeros((100, 100), dtype=bool)
    mask[20:30, 10:20] = True
    bbox, outside = SAM2VideoTracker._mask_to_bbox_norm(mask, w=100, h=100)
    assert outside is False
    assert bbox["x"] == pytest.approx(0.10)
    assert bbox["y"] == pytest.approx(0.20)
    assert bbox["w"] == pytest.approx(0.10)
    assert bbox["h"] == pytest.approx(0.10)


def test_mask_to_bbox_norm_empty_is_outside():
    mask = np.zeros((50, 50), dtype=bool)
    bbox, outside = SAM2VideoTracker._mask_to_bbox_norm(mask, w=50, h=50)
    assert outside is True
    assert bbox == {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


def test_mask_to_bbox_norm_clamps_to_unit():
    mask = np.ones((10, 10), dtype=bool)
    bbox, outside = SAM2VideoTracker._mask_to_bbox_norm(mask, w=10, h=10)
    assert outside is False
    assert bbox == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def test_masks_to_bool_thresholds_all_objs():
    # logits [num_obj=2, 1, H=2, W=2] → [num_obj, H, W] bool (>0 前景), 保留所有对象。
    logits = torch.tensor(
        [
            [[[1.0, -1.0], [-2.0, 3.0]]],
            [[[5.0, 5.0], [5.0, 5.0]]],
        ]
    )
    masks = SAM2VideoTracker._masks_to_bool(logits)
    assert masks.shape == (2, 2, 2)
    assert masks[0].tolist() == [[True, False], [False, True]]
    assert masks[1].tolist() == [[True, True], [True, True]]


def test_obj_ids_to_list_from_tensor_and_list():
    assert SAM2VideoTracker._obj_ids_to_list(torch.tensor([1, 2, 5])) == [1, 2, 5]
    assert SAM2VideoTracker._obj_ids_to_list([3, 4]) == [3, 4]
    assert SAM2VideoTracker._obj_ids_to_list(None) == [1]


# ---------- 窗内传播 (fake predictor, 多目标) ----------


class _FakePropagatePredictor:
    """记录调用 + 按预设 mask 序列逐对象 yield (local_frame, obj_ids, masks)。

    frame_masks 每帧的值:
      - np.ndarray → 单对象 mask (所有已播种 obj 共用该 mask, 用于单目标测试);
      - dict {obj_id: mask} → 逐对象 mask。
    frame_score_logits: {local_idx: logit}, 写进首个 obj 的 output_dict_per_obj 模拟 A1 分数。
    """

    def __init__(self, frame_masks, frame_score_logits=None):
        self._frame_masks = frame_masks
        self._frame_score_logits = frame_score_logits or {}
        self.add_calls: list[dict] = []
        self.reset_called = False
        self._obj_ids: list[int] = []

    def init_state(self, video_path=None, **kw):
        return {
            "video_path": video_path,
            "obj_id_to_idx": {},
            "output_dict_per_obj": {},
        }

    def add_new_points_or_box(
        self,
        *,
        inference_state,
        frame_idx,
        obj_id,
        points=None,
        labels=None,
        box=None,
        normalize_coords=True,
    ):
        self.add_calls.append(
            {
                "frame_idx": frame_idx,
                "obj_id": obj_id,
                "points": points,
                "labels": labels,
                "box": box,
                "normalize": normalize_coords,
            }
        )
        if obj_id not in inference_state["obj_id_to_idx"]:
            idx = len(inference_state["obj_id_to_idx"])
            inference_state["obj_id_to_idx"][obj_id] = idx
            inference_state["output_dict_per_obj"][idx] = {
                "cond_frame_outputs": {},
                "non_cond_frame_outputs": {},
            }
        if obj_id not in self._obj_ids:
            self._obj_ids.append(obj_id)

    def add_new_mask(
        self,
        *,
        inference_state,
        frame_idx,
        obj_id,
        mask,
    ):
        self.add_calls.append(
            {
                "frame_idx": frame_idx,
                "obj_id": obj_id,
                "mask": mask,
            }
        )
        if obj_id not in inference_state["obj_id_to_idx"]:
            idx = len(inference_state["obj_id_to_idx"])
            inference_state["obj_id_to_idx"][obj_id] = idx
            inference_state["output_dict_per_obj"][idx] = {
                "cond_frame_outputs": {},
                "non_cond_frame_outputs": {},
            }
        if obj_id not in self._obj_ids:
            self._obj_ids.append(obj_id)

    def propagate_in_video(
        self,
        inference_state,
        start_frame_idx=None,
        max_frame_num_to_track=None,
        reverse=False,
    ):
        order = sorted(self._frame_masks, reverse=reverse)
        if start_frame_idx is not None:
            end = (
                start_frame_idx - (max_frame_num_to_track or len(order))
                if reverse
                else start_frame_idx + (max_frame_num_to_track or len(order))
            )
            order = [
                frame
                for frame in order
                if min(start_frame_idx, end) <= frame <= max(start_frame_idx, end)
            ]
        obj_ids = self._obj_ids or [1]
        for local_idx in order:
            entry = self._frame_masks[local_idx]
            per_obj = []
            for oid in obj_ids:
                m = entry[oid] if isinstance(entry, dict) else entry
                per_obj.append(np.where(m, 10.0, -10.0))
            logits = torch.from_numpy(np.stack(per_obj)).float()[
                :, None
            ]  # [num_obj,1,H,W]
            if local_idx in self._frame_score_logits:
                idx0 = inference_state["obj_id_to_idx"][obj_ids[0]]
                inference_state["output_dict_per_obj"][idx0]["non_cond_frame_outputs"][
                    local_idx
                ] = {
                    "object_score_logits": torch.tensor(
                        [[self._frame_score_logits[local_idx]]]
                    )
                }
            yield local_idx, list(obj_ids), logits

    def reset_state(self, inference_state):
        self.reset_called = True


def _make_tracker(fake_predictor) -> SAM2VideoTracker:
    inst = SAM2VideoTracker.__new__(SAM2VideoTracker)
    inst.sam_variant = "tiny"
    inst.max_window_frames = 300
    inst.max_context_frames = 1200
    inst.device = "cpu"
    inst.cleanup_uncertain = False
    inst._predictor = fake_predictor
    inst.active_sessions = 0
    return inst


def _seed(obj_id, box):
    return {"obj_id": obj_id, "bbox": box}


def test_mkdtemp_failure_releases_active_session(monkeypatch):
    fake = _FakePropagatePredictor({})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        "video_predictor.tempfile.mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("tmp unavailable")),
    )

    with pytest.raises(OSError, match="tmp unavailable"):
        tracker.propagate(
            video_path="/tmp/x.mp4",
            from_frame=0,
            to_frame=0,
            direction="forward",
            seeds=[_seed(1, {"x": 0, "y": 0, "w": 1, "h": 1})],
        )
    assert tracker.active_sessions == 0


def test_reset_failure_marks_cleanup_uncertain(monkeypatch):
    mask = np.ones((2, 2), dtype=bool)
    fake = _FakePropagatePredictor({0: mask})
    fake.reset_state = lambda _state: (_ for _ in ()).throw(
        RuntimeError("reset failed")
    )
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (2, 2, 1)),
    )
    tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=0,
        direction="forward",
        seeds=[_seed(1, {"x": 0, "y": 0, "w": 1, "h": 1})],
    )
    assert tracker.cleanup_uncertain is True


def test_propagate_forward_maps_local_to_source_frames(monkeypatch):
    fg = np.zeros((100, 100), dtype=bool)
    fg[10:20, 10:20] = True
    frame_masks = {0: fg, 1: fg, 2: np.zeros((100, 100), dtype=bool)}
    fake = _FakePropagatePredictor(frame_masks)
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (100, 100, 3)),
    )

    results = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=10,
        to_frame=12,
        direction="forward",
        seeds=[_seed(1, {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1})],
    )

    assert [r["frame_index"] for r in results] == [10, 11, 12]
    assert all(r["instance_id"] == "1" for r in results)
    assert results[0]["outside"] is False
    assert results[2]["outside"] is True
    assert results[2]["geometry"] == {
        "type": "bbox",
        "x": 0.0,
        "y": 0.0,
        "w": 0.0,
        "h": 0.0,
    }
    # forward seed 锚在窗首帧 (local 0)。
    assert fake.add_calls[0]["frame_idx"] == 0
    assert fake.reset_called is True
    assert tracker.active_sessions == 0


def test_session_initializes_state_once_across_windows(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    fake = _FakePropagatePredictor({index: fg for index in range(6)})
    tracker = _make_tracker(fake)
    init_calls = 0
    original_init = fake.init_state

    def init_state(**kwargs):
        nonlocal init_calls
        init_calls += 1
        return original_init(**kwargs)

    fake.init_state = init_state
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 6)),
    )
    session = tracker.start_session(
        "/tmp/x.mp4",
        10,
        15,
        "forward",
        [_seed(1, {"x": 0, "y": 0, "w": 1, "h": 1})],
    )
    try:
        first = tracker.continue_session(session, 10, 12)
        second = tracker.continue_session(session, 13, 15)
    finally:
        tracker.close_session(session)

    assert init_calls == 1
    assert [row["frame_index"] for row in first] == [10, 11, 12]
    assert [row["frame_index"] for row in second] == [13, 14, 15]


def test_propagate_mask_output_preserves_raw_pixels(monkeypatch):
    mask = np.array([[False, True, False], [True, False, True]], dtype=bool)
    fake = _FakePropagatePredictor({0: mask})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (3, 2, 1)),
    )
    result = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=0,
        direction="forward",
        seeds=[_seed(1, {"x": 0, "y": 0, "w": 1, "h": 1})],
        output_geometry="mask",
    )[0]
    assert result["outside"] is False
    assert result["geometry"] == {
        "type": "mask",
        "rle": {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
    }


def test_propagate_backward_anchors_seed_at_window_end(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    frame_masks = {0: fg, 1: fg, 2: fg}
    fake = _FakePropagatePredictor(frame_masks)
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 3)),
    )

    results = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=10,
        to_frame=12,
        direction="backward",
        seeds=[_seed(1, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})],
    )

    assert [r["frame_index"] for r in results] == [12, 11, 10]
    assert fake.add_calls[0]["frame_idx"] == 2


def test_propagate_multi_obj_emits_per_instance(monkeypatch):
    # 两个目标各占一半; 窗 [0,1]。obj 1 = 左半, obj 2 = 右半。
    left = np.zeros((10, 20), dtype=bool)
    left[:, :10] = True
    right = np.zeros((10, 20), dtype=bool)
    right[:, 10:] = True
    frame_masks = {0: {1: left, 2: right}, 1: {1: left, 2: right}}
    fake = _FakePropagatePredictor(frame_masks)
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (20, 10, 2)),
    )

    results = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=1,
        direction="forward",
        seeds=[
            _seed(1, {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}),
            _seed(2, {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}),
        ],
    )

    # 每帧每对象各一条 → 2 帧 × 2 obj = 4 条。
    assert len(results) == 4
    assert {r["instance_id"] for r in results} == {"1", "2"}
    # obj 1 落在左半 (x≈0), obj 2 落在右半 (x≈0.5)。
    obj1 = [r for r in results if r["instance_id"] == "1"][0]
    obj2 = [r for r in results if r["instance_id"] == "2"][0]
    assert obj1["geometry"]["x"] == pytest.approx(0.0, abs=1e-6)
    assert obj2["geometry"]["x"] == pytest.approx(0.5, abs=0.05)
    # 两个目标各在其种子帧被播种 (obj_id 1/2)。
    assert {c["obj_id"] for c in fake.add_calls} == {1, 2}


def test_propagate_point_seed_passes_points_not_box(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    fake = _FakePropagatePredictor({0: fg})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 1)),
    )

    tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=0,
        direction="forward",
        seeds=[{"obj_id": 1, "points": [[0.5, 0.5, 1], [0.2, 0.2, 0]]}],
    )

    call = fake.add_calls[0]
    assert call["box"] is None
    # 归一化点 → 像素 (10×10): (5,5) 正点 / (2,2) 负点。
    assert call["points"].tolist() == [[5.0, 5.0], [2.0, 2.0]]
    assert call["labels"].tolist() == [1, 0]


def test_propagate_multi_frame_prompts_seeds_each_frame(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    fake = _FakePropagatePredictor({0: fg, 1: fg, 2: fg})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 3)),
    )

    # 窗 [0,2]; obj 1 在源帧 0 (基准框) + 源帧 2 (修正点) 各播种。
    tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=2,
        direction="forward",
        seeds=[
            {
                "obj_id": 1,
                "prompts": [
                    {
                        "frame_index": 0,
                        "bbox": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                    },
                    {"frame_index": 2, "points": [[0.5, 0.5, 1]]},
                ],
            }
        ],
    )

    frames_seeded = sorted(c["frame_idx"] for c in fake.add_calls)
    assert frames_seeded == [0, 2]  # frame_index-lo = 局部帧 0 与 2


def test_propagate_consumes_exact_mask_correction_seed(monkeypatch):
    fg = np.ones((2, 3), dtype=bool)
    fake = _FakePropagatePredictor({0: fg})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (3, 2, 1)),
    )

    tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=8,
        to_frame=8,
        direction="forward",
        seeds=[
            {
                "obj_id": 7,
                "prompts": [
                    {
                        "frame_index": 8,
                        "mask_prompt": {
                            "rle": {
                                "encoding": "coco_rle",
                                "size": [2, 3],
                                "counts": [1, 2, 2, 1],
                            },
                            "source_annotation_id": "annotation-1",
                            "source_version": 3,
                            "source_digest": "a" * 64,
                        },
                    }
                ],
            }
        ],
        output_geometry="mask",
    )

    call = fake.add_calls[0]
    assert call["frame_idx"] == 0
    assert call["obj_id"] == 7
    assert call["mask"].dtype == torch.bool
    assert call["mask"].cpu().numpy().astype(np.uint8).tolist() == [
        [0, 1, 0],
        [1, 0, 1],
    ]


def test_propagate_rejects_mask_seed_with_wrong_frame_size(monkeypatch):
    fake = _FakePropagatePredictor({0: np.ones((2, 3), dtype=bool)})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (4, 2, 1)),
    )

    with pytest.raises(ValueError, match="must match video frame"):
        tracker.propagate(
            video_path="/tmp/x.mp4",
            from_frame=0,
            to_frame=0,
            direction="forward",
            seeds=[
                {
                    "obj_id": 1,
                    "mask_prompt": {
                        "rle": {
                            "encoding": "coco_rle",
                            "size": [2, 3],
                            "counts": [1, 2, 2, 1],
                        },
                        "source_annotation_id": "annotation-1",
                        "source_version": 3,
                        "source_digest": "a" * 64,
                    },
                }
            ],
        )

    assert fake.add_calls == []


def test_propagate_rejects_empty_seeds():
    fake = _FakePropagatePredictor({})
    tracker = _make_tracker(fake)
    with pytest.raises(ValueError, match="at least one seed"):
        tracker.propagate(
            video_path="/tmp/x.mp4",
            from_frame=0,
            to_frame=1,
            direction="forward",
            seeds=[],
        )


def test_propagate_rejects_oversized_window(monkeypatch):
    fake = _FakePropagatePredictor({})
    tracker = _make_tracker(fake)
    tracker.max_window_frames = 5
    with pytest.raises(ValueError, match="exceeds"):
        tracker.propagate(
            video_path="/tmp/x.mp4",
            from_frame=0,
            to_frame=100,
            direction="forward",
            seeds=[_seed(1, {"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1})],
        )


# ---------- A1 · 真实 object score 作 confidence ----------


def test_object_score_reads_logit_and_sigmoids():
    state = {
        "obj_id_to_idx": {1: 0},
        "output_dict_per_obj": {
            0: {
                "cond_frame_outputs": {},
                "non_cond_frame_outputs": {
                    5: {"object_score_logits": torch.tensor([[0.0]])}
                },
            }
        },
    }
    assert SAM2VideoTracker._object_score(state, 1, 5) == pytest.approx(0.5)


def test_object_score_missing_structure_returns_none():
    assert SAM2VideoTracker._object_score({}, 1, 0) is None
    assert SAM2VideoTracker._object_score({"obj_id_to_idx": {1: 0}}, 1, 0) is None


def test_propagate_uses_object_score_as_confidence(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    fake = _FakePropagatePredictor({0: fg, 1: fg}, frame_score_logits={0: 4.0, 1: -3.0})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 2)),
    )
    results = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=1,
        direction="forward",
        seeds=[_seed(1, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})],
    )
    by_frame = {r["frame_index"]: r for r in results}
    assert by_frame[0]["confidence"] == pytest.approx(0.982, abs=1e-2)
    assert by_frame[1]["confidence"] == pytest.approx(0.047, abs=1e-2)
    assert by_frame[0]["outside"] is False
    assert by_frame[1]["outside"] is False


def test_propagate_falls_back_to_one_when_no_object_score(monkeypatch):
    fg = np.ones((10, 10), dtype=bool)
    fake = _FakePropagatePredictor({0: fg})
    tracker = _make_tracker(fake)
    monkeypatch.setattr(
        SAM2VideoTracker,
        "_extract_window_jpegs",
        staticmethod(lambda *a, **k: (10, 10, 1)),
    )
    results = tracker.propagate(
        video_path="/tmp/x.mp4",
        from_frame=0,
        to_frame=0,
        direction="forward",
        seeds=[_seed(1, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})],
    )
    assert results[0]["confidence"] == 1.0
    assert results[0]["outside"] is False
