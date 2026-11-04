"""Async client for Mask revision reads and retention writes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .mask_revision_contract import (
    MaskRevisionCommand,
    decode_mask_revision_reply,
    mask_revision_command_json,
)


class MaskRevisionDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: MaskRevisionCommand):
        response = await self._execute(mask_revision_command_json(request))
        return decode_mask_revision_reply(request, response)
