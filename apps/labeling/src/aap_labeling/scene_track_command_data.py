"""Callback-backed SceneTrack command port that preserves caller transactions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .scene_track_command_contract import (
    SceneTrackCommand,
    SceneTrackCommandData,
    decode_scene_track_reply,
    scene_track_command_json,
)


class SceneTrackCommandDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: SceneTrackCommand) -> SceneTrackCommandData:
        return decode_scene_track_reply(
            request, await self._execute(scene_track_command_json(request))
        )


__all__ = ["SceneTrackCommandDataClient"]
