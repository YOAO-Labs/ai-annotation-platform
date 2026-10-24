"""Async client for the closed Mask edit owner command."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .mask_edit_contract import (
    MaskEditCommand,
    decode_mask_edit_reply,
    mask_edit_command_json,
)


class MaskEditDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: MaskEditCommand):
        response = await self._execute(mask_edit_command_json(request))
        return decode_mask_edit_reply(request, response)
