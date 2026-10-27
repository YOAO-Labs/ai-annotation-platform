"""Explicit per-attempt adapters for the shared media publication lifecycle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType


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
    # Import on selection, so an unrelated format does not need its adapter or
    # mutate a process-global protocol while another attempt is running.
    if kind == "lidar":
        from app.services.exporting import lidar_inputs, lidar_jobs
        from app.services.exporting.lidar_delivery import prepare
        from app.services.exporting.lidar_media_paths import logical_sources
        from app.services.exporting.lidar_preflight import assert_frozen_lidar_ready

        return MediaProtocol(
            "lidar",
            2,
            "lidar_media",
            "lidar_media",
            lidar_inputs,
            lidar_jobs,
            logical_sources,
            prepare,
            assert_frozen_lidar_ready,
        )
    if kind == "image":
        from app.services.exporting import image_inputs, image_jobs
        from app.services.exporting.image_delivery import prepare
        from app.services.exporting.image_media_paths import logical_sources

        return MediaProtocol(
            "image",
            3,
            "image_media",
            "image_media",
            image_inputs,
            image_jobs,
            logical_sources,
            prepare,
        )
    if kind == "video":
        from app.services.exporting import video_inputs, video_jobs
        from app.services.exporting.video_delivery import prepare
        from app.services.exporting.video_media_paths import logical_sources

        return MediaProtocol(
            "video",
            4,
            "video_media",
            "video_media",
            video_inputs,
            video_jobs,
            logical_sources,
            prepare,
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
