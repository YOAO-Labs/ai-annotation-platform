"""Callback-backed client for Labeling prediction partition commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .prediction_partition_contract import (
    EnsurePredictionPartition,
    EnsurePredictionPartitionData,
    decode_prediction_partition_reply,
    prediction_partition_command_json,
)


class PredictionPartitionDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[Any]]) -> None:
        self._execute = execute

    async def ensure(
        self, request: EnsurePredictionPartition
    ) -> EnsurePredictionPartitionData:
        reply = await self._execute(prediction_partition_command_json(request))
        return decode_prediction_partition_reply(request, reply)


__all__ = ["PredictionPartitionDataClient"]
