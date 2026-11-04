"""Callback-based Labeling task-lock client with no database ownership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .task_locks_contract import (
    TaskLocksCommand,
    decode_task_locks_command,
    decode_task_locks_reply,
    task_locks_command_payload,
)

TaskLocksCall = Callable[[Mapping[str, object]], Awaitable[Any]]


class TaskLocksData:
    """Call one fixed Labeling capability through the caller's chosen data port."""

    def __init__(self, call: TaskLocksCall):
        self._call = call

    async def execute(self, value: TaskLocksCommand | object) -> dict[str, object]:
        command = decode_task_locks_command(value)
        raw = await self._call(task_locks_command_payload(command))
        return decode_task_locks_reply(command, raw)


__all__ = ["TaskLocksCall", "TaskLocksData"]
