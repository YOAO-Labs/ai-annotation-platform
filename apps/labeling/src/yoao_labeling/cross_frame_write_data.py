"""Callback-backed client for Labeling cross-frame write commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .cross_frame_write_contract import (
    CrossFrameWriteCommand,
    CrossFrameWriteData,
    cross_frame_write_command_json,
    decode_cross_frame_write_reply,
)


class CrossFrameWriteDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: CrossFrameWriteCommand) -> CrossFrameWriteData:
        reply = await self._execute(cross_frame_write_command_json(request))
        return decode_cross_frame_write_reply(request, reply)


__all__ = ["CrossFrameWriteDataClient"]
