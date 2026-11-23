"""SQLAlchemy call binding for the fixed Audit maintenance capability."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text

from .maintenance_contract import MAINTENANCE_FUNCTION
from .maintenance_data import AuditMaintenanceData


class AuditMaintenanceSession(Protocol):
    no_autoflush: Any

    async def scalar(self, statement, parameters=None) -> Any: ...


class AuditMaintenancePostgresData(AuditMaintenanceData):
    """Call the owner function in the caller transaction without implicit flush."""

    def __init__(self, session: AuditMaintenanceSession):
        self.session = session
        super().__init__(self._call)

    async def _call(self, request: dict[str, Any]) -> Any:
        statement = text(
            f"SELECT {MAINTENANCE_FUNCTION}(CAST(:request AS pg_catalog.jsonb))"
        )
        with self.session.no_autoflush:
            return await self.session.scalar(
                statement,
                {"request": json.dumps(request, ensure_ascii=False)},
            )


async def platform_maintenance(
    session: AuditMaintenanceSession,
    operation: Literal[
        "ensure",
        "lock_source",
        "open_source",
        "lock_retirement",
        "retire",
        "refresh_daily",
    ],
    *,
    month: date | None = None,
    parent_oid: int | None = None,
    source_oid: int | None = None,
    cursor: str | None = None,
    output_id: UUID | None = None,
    published_at: datetime | None = None,
) -> None:
    await session.scalar(
        text(
            "SELECT yoao_audit_v1.platform_maintenance("
            "CAST(:operation AS pg_catalog.text), CAST(:month AS pg_catalog.date), "
            "CAST(:parent_oid AS pg_catalog.int8), CAST(:source_oid AS pg_catalog.int8), "
            "CAST(:cursor AS pg_catalog.text), CAST(:output_id AS pg_catalog.uuid), "
            "CAST(:published_at AS pg_catalog.timestamptz))"
        ),
        {
            "operation": operation,
            "month": month,
            "parent_oid": parent_oid,
            "source_oid": source_oid,
            "cursor": cursor,
            "output_id": output_id,
            "published_at": published_at,
        },
    )
