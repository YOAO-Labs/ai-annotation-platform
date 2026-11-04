"""Transaction callback client for Annotation transfer commands."""

from collections.abc import Awaitable, Callable

from .annotation_transfer_contract import (
    AnnotationTransferCommand,
    AnnotationTransferData,
    annotation_transfer_command_json,
    decode_annotation_transfer_reply,
)


class AnnotationTransferDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(
        self, request: AnnotationTransferCommand
    ) -> AnnotationTransferData:
        return decode_annotation_transfer_reply(
            request, await self._execute(annotation_transfer_command_json(request))
        )
