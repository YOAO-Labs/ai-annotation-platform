"""Callback-backed data client that preserves the caller's transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .task_workflow_contract import (
    TaskWorkflowCommand,
    TaskWorkflowData,
    decode_task_workflow_reply,
    task_workflow_command_json,
)


class LabelingTaskWorkflowData:
    """Validate fixed task commands around an injected transaction callback."""

    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: TaskWorkflowCommand) -> TaskWorkflowData:
        request_json = task_workflow_command_json(request)
        reply = await self._execute(request_json)
        return decode_task_workflow_reply(request, reply)
