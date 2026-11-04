"""Transaction callback binding for Annotation prediction writes."""

from collections.abc import Awaitable, Callable

from .annotation_prediction_contract import (
    AnnotationPredictionCommand,
    AnnotationPredictionData,
    annotation_prediction_command_json,
    decode_annotation_prediction_reply,
)


class AnnotationPredictionDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(
        self, request: AnnotationPredictionCommand
    ) -> AnnotationPredictionData:
        reply = await self._execute(annotation_prediction_command_json(request))
        return decode_annotation_prediction_reply(request, reply)
