"""SQLAlchemy call binding for the fixed Audit maintenance capability."""

from __future__ import annotations

import json
from typing import Any, Protocol

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
