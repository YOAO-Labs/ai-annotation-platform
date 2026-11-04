"""Callback-backed read port for Labeling scene data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .scene_read_contract import (
    SceneReadData,
    SceneReadRequest,
    decode_scene_read_reply,
    scene_read_request_json,
)


class SceneReadDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def query(self, request: SceneReadRequest) -> SceneReadData:
        return decode_scene_read_reply(
            request, await self._execute(scene_read_request_json(request))
        )


__all__ = ["SceneReadDataClient"]
