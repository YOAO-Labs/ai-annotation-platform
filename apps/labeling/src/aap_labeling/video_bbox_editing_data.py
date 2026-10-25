"""Callback-backed client for caller-transaction video-bbox commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .video_bbox_editing_contract import (
    VideoBBoxEditingCommand,
    VideoBBoxEditingData,
    decode_video_bbox_editing_reply,
    video_bbox_editing_command_json,
)


class VideoBBoxEditingDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: VideoBBoxEditingCommand) -> VideoBBoxEditingData:
        reply = await self._execute(video_bbox_editing_command_json(request))
        return decode_video_bbox_editing_reply(request, reply)


__all__ = ["VideoBBoxEditingDataClient"]
