"""Callback-backed data port for Labeling bulk Annotation commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .annotation_bulk_contract import (
    AnnotationBulkCommand,
    AnnotationBulkResult,
    annotation_bulk_command_json,
    decode_annotation_bulk_reply,
)


class AnnotationBulkDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: AnnotationBulkCommand) -> AnnotationBulkResult:
        reply = await self._execute(annotation_bulk_command_json(request))
        return decode_annotation_bulk_reply(request, reply)


__all__ = ["AnnotationBulkDataClient"]
