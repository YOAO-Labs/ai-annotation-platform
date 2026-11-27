"""Explicit per-attempt adapters for the shared media publication lifecycle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType

from app.services.exporting import (
    image_delivery,
    image_inputs,
    image_jobs,
    image_media_paths,
    lidar_delivery,
    lidar_inputs,
    lidar_jobs,
    lidar_media_paths,
    lidar_preflight,
    video_delivery,
    video_inputs,
    video_jobs,
    video_media_paths,
)


@dataclass(frozen=True)
class MediaProtocol:
    kind: str
    schema: int
    proof_key: str
    context_keyword: str
    inputs: ModuleType
    jobs: ModuleType
    logical_sources: Callable
    prepare: Callable
    ready: Callable[..., Awaitable] | None = None

    def error(self, name):
        return f"{self.kind}_export_{name}"


def protocol(kind="lidar"):
    if kind == "lidar":
        return MediaProtocol(
            "lidar",
            2,
            "lidar_media",
            "lidar_media",
            lidar_inputs,
            lidar_jobs,
            lidar_media_paths.logical_sources,
            lidar_delivery.prepare,
            lidar_preflight.assert_frozen_lidar_ready,
        )
    if kind == "image":
        return MediaProtocol(
            "image",
            3,
            "image_media",
            "image_media",
            image_inputs,
            image_jobs,
            image_media_paths.logical_sources,
            image_delivery.prepare,
        )
    if kind == "video":
        return MediaProtocol(
            "video",
            4,
            "video_media",
            "video_media",
            video_inputs,
            video_jobs,
            video_media_paths.logical_sources,
            video_delivery.prepare,
        )
    raise ValueError("Unknown export media protocol")


def for_snapshot(snapshot):
    if type(snapshot) is dict and type(snapshot.get("schema")) is int:
        if snapshot["schema"] == 4:
            return protocol("video")
        if snapshot["schema"] == 3:
            return protocol("image")
        if snapshot["schema"] in {1, 2}:
            return protocol("lidar")
    raise ValueError("Unknown export media snapshot")
