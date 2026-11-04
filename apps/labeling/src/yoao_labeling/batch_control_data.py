"""Callback-backed data port for Labeling batch-control commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .batch_control_contract import (
    BatchControlCommand,
    BatchControlResult,
    batch_control_command_json,
    decode_batch_control_reply,
)


class LabelingBatchControlData:
    """Validate fixed batch commands around the caller's transaction callback."""

    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: BatchControlCommand) -> BatchControlResult:
        request_json = batch_control_command_json(request)
        reply = await self._execute(request_json)
        return decode_batch_control_reply(request, reply)
