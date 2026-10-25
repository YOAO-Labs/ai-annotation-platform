"""Callback-backed data client for bounded screenshot maintenance commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .screenshot_operations_contract import (
    ScreenshotOperationsCommand,
    ScreenshotOperationsResult,
    decode_screenshot_operations_result,
    screenshot_operations_command_json,
)


class ScreenshotOperationsDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(
        self, request: ScreenshotOperationsCommand
    ) -> ScreenshotOperationsResult:
        reply = await self._execute(screenshot_operations_command_json(request))
        return decode_screenshot_operations_result(request, reply)


__all__ = ["ScreenshotOperationsDataClient"]
