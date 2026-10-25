"""Callback-backed client for Labeling Prediction record commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .prediction_record_contract import (
    PredictionRecordCommand,
    PredictionRecordData,
    decode_prediction_record_reply,
    prediction_record_command_json,
)


class PredictionRecordDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def command(self, request: PredictionRecordCommand) -> PredictionRecordData:
        reply = await self._execute(prediction_record_command_json(request))
        return decode_prediction_record_reply(request, reply)


__all__ = ["PredictionRecordDataClient"]
