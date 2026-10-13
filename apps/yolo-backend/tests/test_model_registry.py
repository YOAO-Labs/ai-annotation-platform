"""MODEL_MATRIX + resolve_weight_filename 单测.

矩阵基于 ultralytics/assets v8.3.0 + v8.4.0 实际预训练权重 (2026-06-08 核对). 这套
测试是矩阵的"金本位": 跟 GH release 任何 drift 都会被这里抓到.
"""

from __future__ import annotations

import pytest

from model_registry import (
    MODEL_MATRIX,
    OPENVOCAB_SERIES,
    UnsupportedVariantError,
    is_openvocab_series,
    is_openvocab_supported,
    is_supported,
    iter_supported_combinations,
    openvocab_family,
    resolve_openvocab_weight_filename,
    resolve_weight_filename,
    series_options_for_task,
    sizes_for,
)


def test_matrix_covers_four_tasks_plus_tracker_alias() -> None:
    # tracker 别名到 detection 权重矩阵 (追踪不换权重, 只换关联算法), 与四个真实
    # 权重 task 并列; 别名共享同一 series→sizes 对象。
    assert set(MODEL_MATRIX.keys()) == {
        "detection",
        "segmentation",
        "keypoint",
        "obb",
        "tracker",
    }
    assert MODEL_MATRIX["tracker"] is MODEL_MATRIX["detection"]


def test_detection_has_all_seven_series() -> None:
    assert set(MODEL_MATRIX["detection"].keys()) == {
        "yolov8",
        "yolov9",
        "yolov10",
        "yolo11",
        "yolo12",
        "yolo26",
        "rtdetr",
    }


def test_segmentation_excludes_v10_v12_rtdetr() -> None:
    """v10/v12/rtdetr 官方仅放出 det 权重, 不出 -seg."""
    series_set = set(MODEL_MATRIX["segmentation"].keys())
    assert "yolov10" not in series_set
    assert "yolo12" not in series_set
    assert "rtdetr" not in series_set


def test_yolov9_seg_only_c_e() -> None:
    """v9 全 size det 都有, 但 -seg 只放 c/e."""
    assert MODEL_MATRIX["segmentation"]["yolov9"] == ("c", "e")
    # det 路径仍 t/s/m/c/e 齐全.
    assert MODEL_MATRIX["detection"]["yolov9"] == ("t", "s", "m", "c", "e")


def test_keypoint_obb_three_series_only() -> None:
    """pose/obb 只 v8/v11/v26 三族; v9/v10/v12/rtdetr 不出 -pose / -obb."""
    for task in ("keypoint", "obb"):
        assert set(MODEL_MATRIX[task].keys()) == {"yolov8", "yolo11", "yolo26"}


def test_rtdetr_only_l_x() -> None:
    assert MODEL_MATRIX["detection"]["rtdetr"] == ("l", "x")


def test_yolov10_includes_b_size() -> None:
    """v10 比其它系列多一个 b (balanced) 档."""
    assert "b" in MODEL_MATRIX["detection"]["yolov10"]


# ── 文件名解析 ───────────────────────────────────────────────────────


def test_filename_detection_v8s() -> None:
    assert resolve_weight_filename("detection", "yolov8", "s") == "yolov8s.pt"


def test_filename_detection_yolo11x() -> None:
    assert resolve_weight_filename("detection", "yolo11", "x") == "yolo11x.pt"


def test_filename_segmentation_yolo11l() -> None:
    assert resolve_weight_filename("segmentation", "yolo11", "l") == "yolo11l-seg.pt"


def test_filename_segmentation_yolov9c() -> None:
    assert resolve_weight_filename("segmentation", "yolov9", "c") == "yolov9c-seg.pt"


def test_filename_pose_yolo26m() -> None:
    assert resolve_weight_filename("keypoint", "yolo26", "m") == "yolo26m-pose.pt"


def test_filename_obb_yolo11s() -> None:
    assert resolve_weight_filename("obb", "yolo11", "s") == "yolo11s-obb.pt"


def test_filename_rtdetr_l() -> None:
    """rtdetr 文件名特殊: 短横连接而非紧接 size."""
    assert resolve_weight_filename("detection", "rtdetr", "l") == "rtdetr-l.pt"


def test_filename_rtdetr_x() -> None:
    assert resolve_weight_filename("detection", "rtdetr", "x") == "rtdetr-x.pt"


def test_filename_tracker_reuses_detection_weight() -> None:
    """tracker 解出 detection 权重文件名 (无独立权重, 无 -track 后缀)。"""
    assert resolve_weight_filename("tracker", "yolo11", "s") == "yolo11s.pt"
    assert resolve_weight_filename("tracker", "rtdetr", "l") == "rtdetr-l.pt"


# ── 不支持组合 ───────────────────────────────────────────────────────


def test_unsupported_yolov10_seg() -> None:
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("segmentation", "yolov10", "s")


def test_unsupported_yolo12_pose() -> None:
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("keypoint", "yolo12", "n")


def test_unsupported_rtdetr_seg() -> None:
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("segmentation", "rtdetr", "l")


def test_unsupported_rtdetr_n_size() -> None:
    """rtdetr 没有 n size."""
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("detection", "rtdetr", "n")


def test_unsupported_yolov9_pose() -> None:
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("keypoint", "yolov9", "c")


def test_unsupported_yolov9_t_seg() -> None:
    """v9 在 seg 任务只有 c/e, t/s/m 不行."""
    with pytest.raises(UnsupportedVariantError):
        resolve_weight_filename("segmentation", "yolov9", "t")


# ── helpers ─────────────────────────────────────────────────────────


def test_is_supported_positive() -> None:
    assert is_supported("detection", "yolo11", "s")
    assert is_supported("obb", "yolo26", "x")


def test_is_supported_negative() -> None:
    assert not is_supported("segmentation", "yolov10", "n")
    assert not is_supported("keypoint", "rtdetr", "l")


def test_iter_supported_combinations_count() -> None:
    """有效组合 (按矩阵手算).

    det:     5(v8) + 5(v9) + 6(v10) + 5(v11) + 5(v12) + 5(v26) + 2(rtdetr) = 33
    seg:     5(v8) + 2(v9) + 5(v11) + 5(v26)                               = 17
    pose:    5(v8) + 5(v11) + 5(v26)                                       = 15
    obb:     5(v8) + 5(v11) + 5(v26)                                       = 15
    tracker: 别名 detection 权重矩阵 (追踪不换权重)                        = 33
    总计:                                                                  = 113
    """
    combos = iter_supported_combinations()
    assert len(combos) == 113
    # 抽样核对.
    assert ("detection", "yolo11", "s") in combos
    assert ("obb", "yolo26", "x") in combos
    assert ("detection", "rtdetr", "l") in combos
    # tracker 复用 detection 权重, 解出同一文件名 (无独立权重下载)。
    assert ("tracker", "yolo11", "s") in combos


def test_series_options_filtered_by_task() -> None:
    assert "rtdetr" in series_options_for_task("detection")
    assert "rtdetr" not in series_options_for_task("segmentation")
    assert "yolov10" not in series_options_for_task("keypoint")


def test_sizes_for_yolov9_det_vs_seg() -> None:
    """v9 在 det 和 seg 任务下的 size 集合不同 — 关键多轴 conditional 验证."""
    det_sizes = sizes_for("detection", "yolov9")
    seg_sizes = sizes_for("segmentation", "yolov9")
    assert det_sizes == ["t", "s", "m", "c", "e"]
    assert seg_sizes == ["c", "e"]


# ── 开集 (open-vocabulary) 权重矩阵金本位 (release v8.4.0 实测可下载) ──


def test_openvocab_series_namespace() -> None:
    assert OPENVOCAB_SERIES == {
        "yolo-worldv2",
        "yolo-world",
        "yoloe-v8",
        "yoloe-11",
        "yoloe-26",
    }


def test_openvocab_family_mapping() -> None:
    assert openvocab_family("yolo-worldv2") == "world"
    assert openvocab_family("yolo-world") == "world"
    assert openvocab_family("yoloe-11") == "yoloe"
    assert openvocab_family("yoloe-26") == "yoloe"


def test_openvocab_series_disjoint_from_closed() -> None:
    """开集 series 不与闭集 MODEL_MATRIX series 命名冲突."""
    closed = {s for by in MODEL_MATRIX.values() for s in by}
    assert OPENVOCAB_SERIES.isdisjoint(closed)
    for s in OPENVOCAB_SERIES:
        assert is_openvocab_series(s)
    assert not is_openvocab_series("yolo11")


def test_openvocab_weight_filenames() -> None:
    """与 ultralytics assets release v8.4.0 实际文件名严格对应."""
    assert (
        resolve_openvocab_weight_filename("yolo-worldv2", "s") == "yolov8s-worldv2.pt"
    )
    assert resolve_openvocab_weight_filename("yolo-world", "x") == "yolov8x-world.pt"
    assert resolve_openvocab_weight_filename("yoloe-v8", "l") == "yoloe-v8l-seg.pt"
    assert resolve_openvocab_weight_filename("yoloe-11", "s") == "yoloe-11s-seg.pt"
    assert resolve_openvocab_weight_filename("yoloe-26", "n") == "yoloe-26n-seg.pt"


def test_openvocab_supported_sizes() -> None:
    # world 仅 s/m/l/x (无 n); yoloe-v8/11 仅 s/m/l; yoloe-26 含 n..x.
    assert is_openvocab_supported("yolo-worldv2", "s")
    assert not is_openvocab_supported("yolo-worldv2", "n")
    assert not is_openvocab_supported("yoloe-11", "x")
    assert is_openvocab_supported("yoloe-26", "x")


def test_openvocab_unsupported_raises() -> None:
    with pytest.raises(UnsupportedVariantError):
        resolve_openvocab_weight_filename("yoloe-11", "x")
    with pytest.raises(UnsupportedVariantError):
        resolve_openvocab_weight_filename("yolo-worldv2", "n")
