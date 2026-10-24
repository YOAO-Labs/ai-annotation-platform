"""Validated calls to the fixed Audit maintenance command."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from .maintenance_contract import (
    AuditMaintenanceUnavailable,
    AuditPartition,
    MAINTENANCE_PROTOCOL,
    _ArchiveEventRequest,
    _DropPartitionRequest,
    _EmptyData,
    _EnsureData,
    _EnsurePartitionRequest,
    _ListPartitionsRequest,
    _PartitionsData,
    _RefreshDailyRequest,
    _SnapshotData,
    _SnapshotPartitionRequest,
    decode_maintenance_reply,
)

MaintenanceCall = Callable[[dict[str, Any]], Awaitable[Any]]


class AuditMaintenanceData:
    """Small data API over one injected, transaction-preserving command call."""

    def __init__(self, call: MaintenanceCall):
        self.call = call

    async def _invoke(self, request, expected_data: type) -> Any:
        request_json = request.model_dump(mode="json", by_alias=True)
        reply = decode_maintenance_reply(await self.call(request_json))
        if not isinstance(reply.data, expected_data):
            raise AuditMaintenanceUnavailable() from None
        return reply.data

    async def list_partitions(self) -> list[AuditPartition]:
        data = await self._invoke(
            _ListPartitionsRequest(schema=MAINTENANCE_PROTOCOL, operation="partitions"),
            _PartitionsData,
        )
        return data.partitions

    async def ensure_partition(self, month: date) -> bool:
        data = await self._invoke(
            _EnsurePartitionRequest(
                schema=MAINTENANCE_PROTOCOL, operation="ensure", month=month
            ),
            _EnsureData,
        )
        return data.created

    async def snapshot_partition(
        self, partition: AuditPartition
    ) -> list[dict[str, Any]]:
        data = await self._invoke(
            _SnapshotPartitionRequest(
                schema=MAINTENANCE_PROTOCOL,
                operation="snapshot",
                partition=partition,
            ),
            _SnapshotData,
        )
        return data.rows

    async def drop_partition(self, partition: AuditPartition) -> None:
        await self._invoke(
            _DropPartitionRequest(
                schema=MAINTENANCE_PROTOCOL,
                operation="drop",
                partition=partition,
            ),
            _EmptyData,
        )

    async def record_archive_event(
        self, partition: AuditPartition, row_count: int, object_key: str
    ) -> None:
        request = _ArchiveEventRequest(
            schema=MAINTENANCE_PROTOCOL,
            operation="archive_event",
            partition=partition,
            row_count=row_count,
            object_key=object_key,
        )
        await self._invoke(request, _EmptyData)

    async def refresh_daily(self) -> None:
        await self._invoke(
            _RefreshDailyRequest(
                schema=MAINTENANCE_PROTOCOL, operation="refresh_daily"
            ),
            _EmptyData,
        )
