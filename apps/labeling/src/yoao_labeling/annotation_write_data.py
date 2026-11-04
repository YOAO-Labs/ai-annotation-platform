"""Callback-backed client for transaction-local Annotation write commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .annotation_write_contract import (
    AnnotationWriteCommand,
    AnnotationWriteData,
    annotation_write_command_json,
    decode_annotation_write_reply,
)


class AnnotationWriteDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: AnnotationWriteCommand) -> AnnotationWriteData:
        reply = await self._execute(annotation_write_command_json(request))
        return decode_annotation_write_reply(request, reply)


__all__ = ["AnnotationWriteDataClient"]
