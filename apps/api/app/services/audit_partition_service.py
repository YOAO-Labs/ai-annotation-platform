"""Compatibility adapter for Audit maintenance and archive API callers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from aap_audit.maintenance_postgres import AuditMaintenancePostgresData
from aap_audit.maintenance_contract import AuditPartition
from aap_audit.partitions import AuditPartitionManager

from app.services.audit import AuditAction, AuditService
from app.services.storage import storage_service


class AuditPartitionService:
    @staticmethod
    def _manager(db: AsyncSession) -> AuditPartitionManager:
        async def record_archive(
            partition: AuditPartition, row_count: int, object_key: str
        ) -> None:
            await AuditService.log(
                db,
                actor=None,
                action=AuditAction.AUDIT_ARCHIVE,
                target_type="audit_partition",
                target_id=partition.name,
                request=None,
                status_code=200,
                detail={
                    "partition": partition.name,
                    "row_count": row_count,
                    "archive_key": object_key,
                    "from": partition.range_from.isoformat(),
                },
            )

        return AuditPartitionManager(
            AuditMaintenancePostgresData(db),
            storage_service,
            record_archive=record_archive,
        )

    @staticmethod
    async def ensure_future_partitions(
        db: AsyncSession, *, months_ahead: int = 3
    ) -> list[str]:
        return await AuditPartitionService._manager(db).ensure_future_partitions(
            months_ahead=months_ahead
        )

    @staticmethod
    async def archive_old_partitions(
        db: AsyncSession, *, retain_months: int = 12
    ) -> dict:
        return await AuditPartitionService._manager(db).archive_old_partitions(
            retain_months=retain_months
        )

    @staticmethod
    async def refresh_daily(db: AsyncSession) -> None:
        await AuditMaintenancePostgresData(db).refresh_daily()

    @staticmethod
    def list_archives() -> list[dict]:
        return AuditPartitionManager(None, storage_service).list_archives()

    @staticmethod
    def read_archive_rows(year: int, month: int) -> list[dict] | None:
        return AuditPartitionManager(None, storage_service).read_archive_rows(
            year, month
        )
