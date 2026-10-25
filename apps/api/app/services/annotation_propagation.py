"""视频轨迹跨帧传播的纯逻辑簇。

从 services/annotation.py 抽出的模块级纯函数与共享上下文 dataclass(无 DB 依赖):
整批传播上下文 _PropagateContext、track_id 生成、bbox/keyframe/outside 区间的几何
处理。annotation.py 经 import 回这些符号自用(端点/类方法照旧调用),
`from app.services.annotation import ...` 旧入口经 re-export 保持不变。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.db.models.task import Task
from aap_labeling.video_bbox_editing_policy import (
    clean_bbox_geometry as _clean_bbox_geometry,  # noqa: F401
    clip_outside_ranges as _clip_outside_ranges,  # noqa: F401
    composition_keyframe as _composition_keyframe,  # noqa: F401
    new_track_id as _new_track_id,  # noqa: F401
    track_visible_keyframes as _track_visible_keyframes,  # noqa: F401
)


@dataclass
class _PropagateContext:
    """propagate_batch 整批共享的预解析上下文。

    一次 batch 内所有框共享同一 source_task / target_task,故 scene/frame、
    目标 axis_convention、源/目标帧 ego pose 对整批恒定。循环外解析一次,逐框
    复用,消除原先逐条 propagate 重复解析造成的 N+1(框数 20-50+ 时尤甚)。
    box_3d 专用字段(axis_convention / pose_src / pose_dst)首次遇到 box_3d 时
    才解析并缓存 —— 2D 几何不付这部分 DB 往返。
    """

    src_task: Task
    target_task: Task
    src_scene_id: uuid.UUID | None
    src_frame_index: int | None
    target_scene_id: uuid.UUID | None
    target_frame_index: int | None
    axis_convention: str | None = None
    pose_src: object | None = None
    pose_dst: object | None = None
    _box3d_resolved: bool = False
