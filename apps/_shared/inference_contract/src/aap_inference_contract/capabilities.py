"""Shared capability extraction limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityLimits:
    video_tracker_window_size_frames: int = 300
    video_tracker_sam3_window_size_frames: int = 16
