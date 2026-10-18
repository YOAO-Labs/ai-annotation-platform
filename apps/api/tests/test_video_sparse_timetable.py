"""v0.10.29 · 长视频 sparse timetable + chunk warmup look-ahead 纯函数单测 (Wave3-F)。

覆盖计划 §1.2 (sparse pts_ms 解析 / 锚点网格 / 锚点行筛选) 与 §1.6 (warmup look-ahead 选择)。
都是纯函数, 不依赖 DB / Celery。
"""

from __future__ import annotations

from app.services.video_frame_service import (
    derive_anchor_frames,
    resolve_pts_ms_sparse,
    select_sparse_anchor_rows,
    warmup_chunk_ids,
)


# ── derive_anchor_frames ─────────────────────────────────────────────


def test_derive_anchor_frames_basic_grid_plus_last():
    # stride=5, frame_count=12 → [0,5,10] 末帧 11 补锚点
    assert derive_anchor_frames(12, 5) == [0, 5, 10, 11]


def test_derive_anchor_frames_last_already_on_grid():
    # frame_count=11 → last=10 恰在网格上, 不重复
    assert derive_anchor_frames(11, 5) == [0, 5, 10]


def test_derive_anchor_frames_zero_frame_count():
    assert derive_anchor_frames(0, 5) == []


def test_derive_anchor_frames_stride_clamped_to_one():
    assert derive_anchor_frames(3, 0) == [0, 1, 2]


def test_derive_anchor_frames_single_frame():
    assert derive_anchor_frames(1, 10) == [0]


# ── resolve_pts_ms_sparse ────────────────────────────────────────────


def test_resolve_sparse_exact_anchor_hit():
    anchors = [(0, 0), (10, 1000), (20, 2000)]
    assert resolve_pts_ms_sparse(0, anchors, fps=10, stride=10) == 0
    assert resolve_pts_ms_sparse(10, anchors, fps=10, stride=10) == 1000


def test_resolve_sparse_linear_interpolation_midpoint():
    anchors = [(0, 0), (10, 1000)]
    # frame 5 居中 → 500ms
    assert resolve_pts_ms_sparse(5, anchors, fps=10, stride=10) == 500


def test_resolve_sparse_interpolation_non_uniform_anchors():
    # 锚点 pts 非匀速 (变帧率): frame 5 在 (0,0)-(10,2000) 之间 → 1000
    anchors = [(0, 0), (10, 2000)]
    assert resolve_pts_ms_sparse(5, anchors, fps=10, stride=10) == 1000


def test_resolve_sparse_vfr_non_keyframe_gap():
    anchors = [(2, 100), (8, 550)]
    assert resolve_pts_ms_sparse(4, anchors, fps=30, stride=1) == 250


def test_resolve_sparse_extrapolate_before_first_anchor():
    # first anchor 在 frame 4 (pts 400), 查 frame 0 → 外推 400 - 4/fps*1000
    anchors = [(4, 400), (14, 1400)]
    assert resolve_pts_ms_sparse(0, anchors, fps=10, stride=10) == 0


def test_resolve_sparse_extrapolate_after_last_anchor():
    anchors = [(0, 0), (10, 1000)]
    # frame 15 在最后锚点之后 → 1000 + 5/fps*1000 = 1500
    assert resolve_pts_ms_sparse(15, anchors, fps=10, stride=10) == 1500


def test_resolve_sparse_empty_anchors_falls_back_to_fps():
    assert resolve_pts_ms_sparse(10, [], fps=10, stride=10) == 1000


def test_resolve_sparse_empty_anchors_no_fps_is_none():
    assert resolve_pts_ms_sparse(10, [], fps=None, stride=10) is None


def test_resolve_sparse_out_of_range_no_fps_is_none():
    anchors = [(0, 0), (10, 1000)]
    # 范围外且无 fps → 无法外推
    assert resolve_pts_ms_sparse(20, anchors, fps=None, stride=10) is None


# ── select_sparse_anchor_rows ────────────────────────────────────────


def _row(idx: int, keyframe: bool = False) -> dict:
    return {"frame_index": idx, "pts_ms": idx * 100, "is_keyframe": keyframe}


def test_select_sparse_stride_one_returns_all():
    rows = [_row(i) for i in range(5)]
    assert select_sparse_anchor_rows(rows, 1) == rows


def test_select_sparse_keeps_grid_anchors():
    rows = [_row(i) for i in range(12)]
    kept = [r["frame_index"] for r in select_sparse_anchor_rows(rows, 5)]
    # 网格 0,5,10 + 末帧 11
    assert kept == [0, 5, 10, 11]


def test_select_sparse_always_keeps_keyframes():
    rows = [_row(i, keyframe=(i == 3)) for i in range(12)]
    kept = [r["frame_index"] for r in select_sparse_anchor_rows(rows, 5)]
    # 关键帧 3 即便不在网格上也保留 (保 smart-copy 对齐判定)
    assert 3 in kept
    assert kept == [0, 3, 5, 10, 11]


def test_select_sparse_empty_rows():
    assert select_sparse_anchor_rows([], 5) == []


def test_select_sparse_preserves_order():
    rows = [_row(i) for i in range(20)]
    kept = select_sparse_anchor_rows(rows, 5)
    indices = [r["frame_index"] for r in kept]
    assert indices == sorted(indices)


# ── warmup_chunk_ids ─────────────────────────────────────────────────


def test_warmup_default_lookahead_one():
    # 请求 chunk [0,1], 末尾 chunk 10 → warmup [2]
    assert warmup_chunk_ids([0, 1], last_chunk_id=10, look_ahead=1) == [2]


def test_warmup_lookahead_multiple():
    assert warmup_chunk_ids([3], last_chunk_id=10, look_ahead=3) == [4, 5, 6]


def test_warmup_stops_at_last_chunk():
    # frontier=9, last=10, look_ahead=3 → 只能 warmup 10
    assert warmup_chunk_ids([9], last_chunk_id=10, look_ahead=3) == [10]


def test_warmup_at_last_chunk_returns_empty():
    assert warmup_chunk_ids([10], last_chunk_id=10, look_ahead=2) == []


def test_warmup_lookahead_zero_disabled():
    assert warmup_chunk_ids([0, 1], last_chunk_id=10, look_ahead=0) == []


def test_warmup_empty_request():
    assert warmup_chunk_ids([], last_chunk_id=10, look_ahead=1) == []


def test_warmup_no_overlap_with_requested():
    # frontier=2, 但请求里已有 3 (跨范围请求) → warmup 不重复 3
    assert warmup_chunk_ids([0, 1, 2, 3], last_chunk_id=10, look_ahead=1) == [4]
