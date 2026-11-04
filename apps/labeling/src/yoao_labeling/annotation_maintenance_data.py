"""Bind draft and schema cleanup commands to the caller's transaction."""

from collections.abc import Awaitable, Callable

from .annotation_maintenance_contract import (
    AnnotationMaintenanceCommand,
    AnnotationMaintenanceData,
    annotation_maintenance_command_json,
    decode_annotation_maintenance_reply,
)


class AnnotationMaintenanceDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(
        self, request: AnnotationMaintenanceCommand
    ) -> AnnotationMaintenanceData:
        return decode_annotation_maintenance_reply(
            request, await self._execute(annotation_maintenance_command_json(request))
        )
