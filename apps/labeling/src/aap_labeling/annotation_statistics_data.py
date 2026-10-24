"""Callback-backed data port for Labeling annotation-statistics commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
import uuid

from .annotation_statistics_contract import (
    AnnotationStatisticsSnapshot,
    annotation_statistics_command,
    annotation_statistics_command_json,
    decode_annotation_statistics_reply,
)


class LabelingAnnotationStatisticsData:
    """Validate one fixed recompute command around the caller's transaction."""

    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def recompute_task(self, task_id: uuid.UUID) -> AnnotationStatisticsSnapshot:
        request = annotation_statistics_command(task_id)
        reply = await self._execute(annotation_statistics_command_json(request))
        return decode_annotation_statistics_reply(request, reply)


__all__ = ["LabelingAnnotationStatisticsData"]
