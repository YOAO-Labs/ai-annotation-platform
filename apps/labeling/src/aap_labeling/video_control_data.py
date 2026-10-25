"""Callback-backed client for human video-tracker control commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .video_control_contract import (
    VideoControlCommand,
    decode_video_control_reply,
    video_control_command_json,
)


class VideoControlDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: VideoControlCommand):
        reply = await self._execute(video_control_command_json(request))
        return decode_video_control_reply(request, reply)


__all__ = ["VideoControlDataClient"]
