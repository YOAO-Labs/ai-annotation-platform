"""Async client for AI Mask acceptance persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .ai_mask_accept_contract import (
    AiMaskAcceptCommand,
    ai_mask_accept_command_json,
    decode_ai_mask_accept_reply,
)


class AiMaskAcceptDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: AiMaskAcceptCommand):
        response = await self._execute(ai_mask_accept_command_json(request))
        return decode_ai_mask_accept_reply(request, response)
