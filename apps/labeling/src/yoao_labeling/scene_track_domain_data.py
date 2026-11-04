"""Callback-based SceneTrack client that preserves the caller's transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .scene_track_domain_contract import (
    SceneTrackDomainCommand,
    SceneTrackDomainData,
    decode_scene_track_reply,
    scene_track_command_json,
)


class SceneTrackDomainDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: SceneTrackDomainCommand) -> SceneTrackDomainData:
        return decode_scene_track_reply(
            request, await self._execute(scene_track_command_json(request))
        )


__all__ = ["SceneTrackDomainDataClient"]
