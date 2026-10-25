"""Bind fixed Task maintenance commands to the caller's transaction."""

from collections.abc import Awaitable, Callable

from .task_maintenance_contract import (
    TaskMaintenanceCommand,
    TaskMaintenanceData,
    decode_task_maintenance_reply,
    task_maintenance_command_json,
)


class TaskMaintenanceDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: TaskMaintenanceCommand) -> TaskMaintenanceData:
        return decode_task_maintenance_reply(
            request, await self._execute(task_maintenance_command_json(request))
        )
