"""Video task context and source-frame sampling primitives."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.schemas.task import VideoMetadata


@dataclass(frozen=True)
class VideoContext:
    item: DatasetItem
    metadata: VideoMetadata
    task: Task | None = None

    @property
    def task_id(self) -> uuid.UUID | None:
        return self.task.id if self.task else None


def derive_step(source_fps: float | None, sampling: dict) -> int:
    """从采样配置派生网格步长 step (源帧空间), 最小为 1。

    - mode="step" → frame_step
    - mode="fps"  → max(1, round(source_fps / target_fps))
    - mode="none" / 缺省 / 配置不全 → 1 (退化为不采样, 所有帧都是网格点)
    """
    if not sampling:
        return 1
    mode = sampling.get("mode", "none")
    if mode == "step":
        frame_step = sampling.get("frame_step")
        if frame_step is None:
            return 1
        return max(1, int(frame_step))
    if mode == "fps":
        target_fps = sampling.get("target_fps")
        if not source_fps or not target_fps:
            return 1
        return max(1, round(source_fps / target_fps))
    return 1


def derive_sampled_frames(frame_count: int, step: int) -> list[int]:
    """绝对网格 (锚定 0) 上的采样帧列表: [0, step, 2*step, ...] 且 < frame_count。"""
    if frame_count <= 0:
        return []
    step = max(1, int(step))
    return list(range(0, frame_count, step))
